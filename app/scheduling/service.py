"""
APScheduler service wiring.

Provides the module-level scheduler that ``app.main``'s lifespan calls
into. Three concerns live here:

  1. Creating the ``AsyncIOScheduler`` with a Redis jobstore (so
     multiple API replicas don't double-fire the same schedule).
  2. Adding/updating/removing jobs to mirror scheduled ``TestFlow``
     rows in the DB.
  3. On startup, loading every enabled scheduled flow and upserting its
     job — this catches rows that were created while the worker was offline.

Why AsyncIOScheduler?
  The API process already runs an asyncio event loop; in-process
  scheduling keeps the deployment a single container. The Redis
  jobstore means a second replica is safe: only one replica picks
  up each fire (APScheduler acquires a lock per job).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC

from sqlalchemy import select

import app.app_state as app_state
from app.models.test_flows import TestFlow
from app.scheduling.runner import run_test_flow

logger = logging.getLogger(__name__)


# ``_scheduler`` is private but read by ``sync_flow_job`` and friends
# below. Tests can patch it to an in-memory fake.
_scheduler = None  # type: ignore[var-annotated]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def start_scheduler(redis_url: str) -> None:
    """Initialise the scheduler and load existing scheduled test flows.

    Called from ``app.main.lifespan`` at startup, after
    ``app_state.db_session_factory`` is ready (the runner uses it).

    Idempotent — calling twice is a no-op (second call is logged and
    ignored).
    """
    global _scheduler
    if _scheduler is not None:
        logger.info("start_scheduler called but scheduler is already running")
        return

    try:
        from apscheduler.executors.asyncio import AsyncIOExecutor
        from apscheduler.jobstores.redis import RedisJobStore
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError as exc:
        logger.warning(
            "APScheduler not installed — scheduled test flows are disabled in this process: %s",
            exc,
        )
        return

    # Parse redis URL into host/port/db — APScheduler's RedisJobStore
    # doesn't accept a URL string.
    host, port, db_index, password = _parse_redis_url(redis_url)

    jobstore_kwargs: dict = {
        "host": host,
        "port": port,
        "db": db_index,
        # Namespace APScheduler's keys so they don't collide with app keys.
        "jobs_key": "amcp:scheduled_jobs",
        "run_times_key": "amcp:scheduled_run_times",
    }
    if password:
        jobstore_kwargs["password"] = password

    try:
        jobstore = RedisJobStore(**jobstore_kwargs)
    except Exception as exc:
        logger.error(
            "Failed to create APScheduler Redis jobstore — scheduled test flows disabled: %s",
            exc,
        )
        return

    _scheduler = AsyncIOScheduler(
        jobstores={"default": jobstore},
        executors={"default": AsyncIOExecutor()},
        job_defaults={
            # Coalesce missed runs: if the worker was down for 3 hours
            # and a daily schedule fired twice in that window, we only
            # run it once on resume.
            "coalesce": True,
            # Grace period for late runs — 1 hour feels right given
            # typical network/restart hiccups.
            "misfire_grace_time": 3600,
            "max_instances": 1,
        },
    )
    _scheduler.start()
    logger.info("Scheduler started (Redis jobstore at %s:%s/%s)", host, port, db_index)

    # Best-effort initial sync — load enabled schedules from the DB and
    # upsert their jobs. This catches rows created while the worker was
    # offline.
    try:
        await _initial_sync()
    except Exception as exc:
        logger.exception("Initial schedule sync failed (non-fatal): %s", exc)


async def stop_scheduler() -> None:
    """Shut down the scheduler cleanly. Safe to call if never started."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception as exc:
        logger.warning("Scheduler shutdown error (ignored): %s", exc)
    _scheduler = None
    logger.info("Scheduler stopped")


async def _initial_sync() -> None:
    """Load every enabled test flow that carries a cron expression and upsert its job."""
    sess_factory = app_state.db_session_factory
    if sess_factory is None:
        logger.warning("initial sync skipped — db_session_factory not set")
        return
    async with sess_factory() as db:
        flow_result = await db.execute(
            select(TestFlow).where(
                TestFlow.enabled.is_(True),
                TestFlow.schedule_cron.is_not(None),
            )
        )
        flows = list(flow_result.scalars().all())
    flow_loaded = 0
    for f in flows:
        try:
            sync_flow_job(f)
            flow_loaded += 1
        except Exception as exc:
            logger.warning("initial sync: skipping test flow %s: %s", f.id, exc)
    logger.info("initial sync loaded %d test-flow job(s)", flow_loaded)


# --------------------------------------------------------------------------- #
# Test-flow job CRUD — called by the test-flow routes
# --------------------------------------------------------------------------- #


def sync_flow_job(flow: TestFlow) -> None:
    """Add or replace the APScheduler job for a scheduled ``TestFlow``.

    The job is removed when the flow is
    disabled or has no ``schedule_cron``. Safe to call from a route handler.
    """
    if _scheduler is None:
        logger.debug("sync_flow_job: scheduler not running, skipping %s", flow.id)
        return

    job_id = _flow_job_id(flow.id)

    if not flow.enabled or not flow.schedule_cron:
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass
        return

    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not importable in sync_flow_job")
        return

    try:
        trigger = CronTrigger.from_crontab(
            flow.schedule_cron,
            timezone=flow.timezone or "UTC",
        )
    except Exception as exc:
        logger.error(
            "sync_flow_job: bad cron '%s' for flow %s: %s",
            flow.schedule_cron,
            flow.id,
            exc,
        )
        return

    _scheduler.add_job(
        _apscheduler_fire_flow,
        trigger=trigger,
        id=job_id,
        name=f"testflow:{flow.name}",
        args=[str(flow.id)],
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info(
        "sync_flow_job: upserted %s (%s, tz=%s)",
        job_id,
        flow.schedule_cron,
        flow.timezone,
    )


def remove_flow_job(flow_id: uuid.UUID | str) -> None:
    """Remove the APScheduler job for ``flow_id`` if present."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_flow_job_id(flow_id))
    except Exception:
        pass


def get_flow_next_run_time(flow_id: uuid.UUID | str):
    """Return the next fire time for a flow's scheduled job (or None)."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(_flow_job_id(flow_id))
    return job.next_run_time if job else None


def compute_flow_next_run(flow: TestFlow):
    """Compute the next fire time for ``flow`` directly from its cron trigger.

    Returned as a naive UTC ``datetime`` (matching the ``utcnow()`` convention
    used elsewhere for ``last_run_at`` / ``next_run_at``). Independent of
    whether the scheduler is running, so routes can persist it even in test
    mode. Returns ``None`` when the flow isn't schedulable or the cron is bad.
    """
    if not flow.enabled or not flow.schedule_cron:
        return None
    try:
        from datetime import datetime

        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(
            flow.schedule_cron,
            timezone=flow.timezone or "UTC",
        )
        now = datetime.now(UTC)
        nxt = trigger.get_next_fire_time(None, now)
        if nxt is None:
            return None
        return nxt.astimezone(UTC).replace(tzinfo=None)
    except Exception as exc:
        logger.warning("compute_flow_next_run failed for flow %s: %s", flow.id, exc)
        return None


async def _apscheduler_fire_flow(flow_id_str: str) -> None:
    """Thin wrapper APScheduler invokes when a test-flow trigger fires."""
    try:
        await run_test_flow(flow_id_str)
    except LookupError as exc:
        # Flow deleted/disabled between the trigger firing and the job running.
        logger.info("scheduled test-flow skipped: %s", exc)
    except Exception:
        logger.exception("scheduled test-flow crashed for %s", flow_id_str)
        raise


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _flow_job_id(flow_id: uuid.UUID | str) -> str:
    return f"testflow:{flow_id}"


def _parse_redis_url(url: str) -> tuple[str, int, int, str | None]:
    """Parse a ``redis://[:password@]host:port/db`` URL into pieces.

    APScheduler's ``RedisJobStore`` takes host/port/db/password as
    separate kwargs rather than a URL, so we do the split here.
    Defaults match the ``redis[asyncio]`` client's own defaults.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url or "redis://localhost:6379/0")
    host = parsed.hostname or "localhost"
    port = int(parsed.port or 6379)
    db_index = 0
    if parsed.path and parsed.path.strip("/"):
        try:
            db_index = int(parsed.path.strip("/"))
        except ValueError:
            db_index = 0
    password = parsed.password or None
    return host, port, db_index, password
