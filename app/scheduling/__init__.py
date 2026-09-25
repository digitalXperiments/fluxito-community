"""
Scheduling package — cron-driven test flows.

Public surface:

  * ``start_scheduler(redis_url)`` / ``stop_scheduler()`` — lifespan hooks
    that bring an ``AsyncIOScheduler`` up/down (``service.py``).
  * ``sync_flow_job(flow)`` / ``remove_flow_job(flow_id)`` — mirror a
    scheduled ``TestFlow`` row into the scheduler.
  * ``cadence_to_cron(cadence, **kwargs)`` — convert preset cadences
    (daily/weekly/monthly) to cron expressions at save time.

Worker imports are kept inside ``runner.py``/``service.py`` rather than
here so that routes can import the public helpers without pulling
APScheduler into the import graph.
"""

from app.scheduling.cron import cadence_to_cron

__all__ = ["cadence_to_cron"]
