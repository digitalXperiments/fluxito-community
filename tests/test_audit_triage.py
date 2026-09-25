# tests/test_audit_triage.py
"""
Audit finding triage (resolve / snooze / reopen).

* Pure unit tests for the fingerprint + effective-status semantics.
* DB-backed route tests (skip without Postgres) for the POST endpoints,
  permission gate, cross-run carry-over and the MCP get_run annotation.
* Migration sanity: 079 chains onto 078 and the versions dir has one head.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.app_state as app_state
from app.models.auditing import AuditFindingTriage, finding_fingerprint
from app.services import audit_triage

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_message_when_rule_id_present():
    a = finding_fingerprint(
        platform="ga4", rule_id="ga4.purchase.tid", event="purchase", message="3 hits missing"
    )
    b = finding_fingerprint(
        platform="ga4", rule_id="ga4.purchase.tid", event="purchase", message="7 hits missing"
    )
    assert a == b
    assert len(a) == 40


def test_fingerprint_distinguishes_entity_and_event():
    base = {"platform": "ga4", "rule_id": "r1"}
    assert finding_fingerprint(**base, event="purchase") != finding_fingerprint(**base, event="add_to_cart")
    assert finding_fingerprint(**base, entity_id="tag-1") != finding_fingerprint(**base, entity_id="tag-2")


def test_fingerprint_uses_normalised_message_without_rule_id():
    a = finding_fingerprint(platform="seo", message="Missing  <title>  tag")
    b = finding_fingerprint(platform="seo", message="missing <title> tag")
    c = finding_fingerprint(platform="seo", message="Missing meta description")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# Effective status
# ---------------------------------------------------------------------------


def _triage(**kw) -> AuditFindingTriage:
    return AuditFindingTriage(project_id=uuid.uuid4(), fingerprint="x", **kw)


def test_resolved_is_sticky_across_runs():
    t = _triage(status="resolved")
    assert t.effective_status(NOW + timedelta(days=90), NOW + timedelta(days=90)) == "resolved"


def test_timed_snooze_expires():
    t = _triage(status="snoozed", snoozed_until=NOW + timedelta(days=7))
    assert t.effective_status(None, NOW + timedelta(days=6)) == "snoozed"
    assert t.effective_status(None, NOW + timedelta(days=8)) == "open"


def test_next_run_snooze_only_covers_that_run_and_older():
    run_at = NOW - timedelta(hours=1)
    t = _triage(status="snoozed", snoozed_run_at=run_at)
    assert t.effective_status(run_at, NOW) == "snoozed"
    assert t.effective_status(run_at - timedelta(days=1), NOW) == "snoozed"
    assert t.effective_status(run_at + timedelta(minutes=5), NOW) == "open"
    # Naive timestamps (test DB columns without tz) are treated as UTC.
    assert t.effective_status(run_at.replace(tzinfo=None), NOW) == "snoozed"


def test_reopened_reads_open():
    assert _triage(status="open").effective_status(None, NOW) == "open"


def test_annotate_and_open_counts():
    fp_res = finding_fingerprint(rule_id="a")
    fp_snz = finding_fingerprint(rule_id="b")
    findings = [
        {"fingerprint": fp_res, "passed": False, "severity": "critical"},
        {"fingerprint": fp_snz, "passed": False, "severity": "warning"},
        {"fingerprint": finding_fingerprint(rule_id="c"), "passed": False, "severity": "warning"},
        {"fingerprint": finding_fingerprint(rule_id="d"), "passed": False, "severity": None},
        {"fingerprint": fp_res, "passed": True, "severity": None},
    ]
    tmap = {
        fp_res: _triage(status="resolved"),
        fp_snz: _triage(status="snoozed", snoozed_until=NOW + timedelta(days=30)),
    }
    audit_triage.annotate_findings(findings, tmap, None, NOW)
    assert [f["triage_status"] for f in findings] == ["resolved", "snoozed", "open", "open", "open"]
    counts = audit_triage.open_counts(findings)
    assert counts == {
        "critical": 0,
        "warning": 1,
        "info": 1,
        "open": 2,
        "resolved": 1,
        "snoozed": 1,
        "hidden": 2,
    }


# ---------------------------------------------------------------------------
# Migration sanity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DB-backed route + MCP tests
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.fixture
async def _client(_patch_db, fake_redis):
    import httpx
    from httpx import ASGITransport

    from app.main import app

    async def _no_cache(*a, **k):
        return None

    with (
        patch.object(app_state, "redis_client", fake_redis),
        patch("app.auth.permissions._cache_get", new=_no_cache),
        patch("app.auth.permissions._cache_set", new=_no_cache),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


async def _seed(db_session_factory, *, rbac: bool = False, role: str = "owner"):
    """User + project (+ membership) + two tag_audit runs with the same findings."""
    from sqlalchemy import select

    from app.models.auditing import AuditFinding, AuditRun
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email=f"triage-{uuid.uuid4().hex[:8]}@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="Triage", slug=f"tri-{uuid.uuid4().hex[:8]}", owner_id=u.id, rbac_enabled=rbac)
        db.add(p)
        await db.flush()
        db.add(ProjectMember(project_id=p.id, user_id=u.id, role=role))
        now = datetime.now(UTC).replace(tzinfo=None)
        runs = []
        for i, created in enumerate([now - timedelta(days=1), now]):
            r = AuditRun(
                project_id=p.id,
                audit_type="tag_audit",
                title=f"run {i}",
                score=70,
                critical_count=1,
                warning_count=1,
                created_by=u.id,
                created_at=created,
            )
            db.add(r)
            await db.flush()
            for sev, rule in (("critical", "ga4.tid"), ("warning", "meta.consent")):
                db.add(
                    AuditFinding(
                        run_id=r.id,
                        project_id=p.id,
                        platform="ga4",
                        severity=sev,
                        rule_id=rule,
                        event="purchase",
                        passed=False,
                        message=f"{rule} failed on run {i}",
                    )
                )
            runs.append(r.id)
        await db.commit()

        rows = (
            await db.execute(
                select(AuditFinding.id, AuditFinding.run_id, AuditFinding.rule_id).where(
                    AuditFinding.project_id == p.id
                )
            )
        ).all()
    fids = {(str(rid), rule): str(fid) for fid, rid, rule in rows}
    return SimpleNamespace(user_id=u.id, project_id=p.id, runs=[str(r) for r in runs], fids=fids)


def _cookie(client, user_id):
    from app.auth.uid_cookie import sign_uid

    client.cookies.set("uid", sign_uid(str(user_id)))


@pytest.mark.asyncio
async def test_resolve_carries_to_later_runs_and_reopen(_client, db_session_factory):
    s = await _seed(db_session_factory)
    _cookie(_client, s.user_id)
    older, latest = s.runs
    q = f"?project_id={s.project_id}"

    # Resolve on the OLDER run → hidden in the latest run too (fingerprint match).
    fid = s.fids[(older, "ga4.tid")]
    r = await _client.post(f"/api/audits/findings/{fid}/resolve{q}", json={"note": "fixed in v42"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["triage_status"] == "resolved"
    assert body["triage"]["note"] == "fixed in v42"
    assert body["triage"]["resolved_by"] == str(s.user_id)

    r = await _client.get(f"/api/audits/{latest}{q}")
    assert r.status_code == 200
    by_rule = {f["rule_id"]: f["triage_status"] for f in r.json()["findings"]}
    assert by_rule == {"ga4.tid": "resolved", "meta.consent": "open"}

    # Reopen → open again everywhere.
    r = await _client.post(f"/api/audits/findings/{s.fids[(latest, 'ga4.tid')]}/reopen{q}")
    assert r.status_code == 200
    assert r.json()["triage_status"] == "open"
    r = await _client.get(f"/api/audits/{latest}{q}")
    assert {f["triage_status"] for f in r.json()["findings"]} == {"open"}

    # One triage row per (project, fingerprint) — upserted, not duplicated.
    from sqlalchemy import func, select

    async with db_session_factory() as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(AuditFindingTriage)
                .where(AuditFindingTriage.project_id == s.project_id)
            )
        ).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_snooze_options(_client, db_session_factory):
    s = await _seed(db_session_factory)
    _cookie(_client, s.user_id)
    older, latest = s.runs
    q = f"?project_id={s.project_id}"

    # "until next run" on the older run: snoozed there, open again in the newer run.
    r = await _client.post(
        f"/api/audits/findings/{s.fids[(older, 'meta.consent')]}/snooze{q}", json={"snooze": "next_run"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["triage"]["snooze_until_next_run"] is True
    old = {
        f["rule_id"]: f["triage_status"]
        for f in (await _client.get(f"/api/audits/{older}{q}")).json()["findings"]
    }
    new = {
        f["rule_id"]: f["triage_status"]
        for f in (await _client.get(f"/api/audits/{latest}{q}")).json()["findings"]
    }
    assert old["meta.consent"] == "snoozed"
    assert new["meta.consent"] == "open"

    # 7-day snooze: hidden in every run until it expires.
    r = await _client.post(
        f"/api/audits/findings/{s.fids[(latest, 'meta.consent')]}/snooze{q}", json={"snooze": "7d"}
    )
    assert r.status_code == 200
    until = datetime.fromisoformat(r.json()["triage"]["snoozed_until"])
    assert timedelta(days=6, hours=23) < until - datetime.now(UTC) <= timedelta(days=7)
    new = {
        f["rule_id"]: f["triage_status"]
        for f in (await _client.get(f"/api/audits/{latest}{q}")).json()["findings"]
    }
    assert new["meta.consent"] == "snoozed"

    # Bad option → 400; unknown finding → 404.
    r = await _client.post(
        f"/api/audits/findings/{s.fids[(latest, 'ga4.tid')]}/snooze{q}", json={"snooze": "99d"}
    )
    assert r.status_code == 400
    r = await _client.post(f"/api/audits/findings/{uuid.uuid4()}/resolve{q}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_detail_page_counts_open_only(_client, db_session_factory):
    s = await _seed(db_session_factory)
    _cookie(_client, s.user_id)
    older, latest = s.runs
    q = f"?project_id={s.project_id}"
    await _client.post(f"/api/audits/findings/{s.fids[(latest, 'ga4.tid')]}/resolve{q}")

    r = await _client.get(f"/audits/run/{latest}{q}")
    assert r.status_code == 200
    html = r.text
    assert '<span id="auIssuesN">1</span>' in html
    assert '<span id="auChipCritical">0</span>' in html
    assert 'Show resolved &amp; snoozed (<span id="auTriagedN">1</span>)' in html
    assert 'data-triage="resolved"' in html
    assert '"canTriage": true' in html or "canTriage: true" in html


@pytest.mark.asyncio
async def test_triage_requires_analysis_write(_client, db_session_factory):
    # RBAC on + plain member with no roles → no analysis:write → 403.
    s = await _seed(db_session_factory, rbac=True, role="member")
    _cookie(_client, s.user_id)
    fid = s.fids[(s.runs[1], "ga4.tid")]
    r = await _client.post(f"/api/audits/findings/{fid}/resolve?project_id={s.project_id}")
    assert r.status_code == 403

    # A signed-in non-member of the project is also rejected (the foreign
    # project id is dropped, so the finding isn't found in their own project).
    other = await _seed(db_session_factory)
    _cookie(_client, other.user_id)
    r = await _client.post(f"/api/audits/findings/{fid}/resolve?project_id={s.project_id}")
    assert r.status_code in (403, 404)

    # Unauthenticated → 401.
    _client.cookies.clear()
    r = await _client.post(f"/api/audits/findings/{fid}/resolve?project_id={s.project_id}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mcp_get_run_reports_triage(_patch_db, db_session_factory):
    s = await _seed(db_session_factory)
    latest = s.runs[1]
    from app.models.auditing import AuditFinding, AuditRun

    async with db_session_factory() as db:
        finding = await db.get(AuditFinding, uuid.UUID(s.fids[(latest, "ga4.tid")]))
        run = await db.get(AuditRun, uuid.UUID(latest))
        assert finding and run
        await audit_triage.set_triage(
            db, project_id=s.project_id, finding=finding, run=run, action="resolve", user_id=s.user_id
        )
        await db.commit()

    from app.tools.save_audit_result_tools import register_save_audit_result_tools

    tools: dict = {}

    class _FakeMCP:
        def tool(self, name):
            def deco(fn):
                tools[name] = fn
                return fn

            return deco

    register_save_audit_result_tools(_FakeMCP())
    user = SimpleNamespace(user_id=str(s.user_id))
    proj = SimpleNamespace(project_id=str(s.project_id))
    token = app_state.current_project_ctx.set(proj)
    try:
        with patch("app.tools.save_audit_result_tools.get_current_user", return_value=user):
            out = await tools["save_audit_result"](action="get_run", run_id=latest)
    finally:
        app_state.current_project_ctx.reset(token)

    assert not out.get("error"), out
    by_rule = {f["rule_id"]: f["triage_status"] for f in out["findings"]}
    assert by_rule == {"ga4.tid": "resolved", "meta.consent": "open"}
    assert out["open_counts"]["open"] == 1
    assert out["open_counts"]["resolved"] == 1
    assert "triage_note" in out
