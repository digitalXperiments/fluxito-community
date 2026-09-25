"""Tests for app.connectors.call_usage (usage from the tool-call activity log)
and the two pages that render it: /settings/connections and the Project
Settings -> API limits tab."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import app.app_state as app_state
from app.connectors import call_usage as cu
from app.connectors import rate_limits as rl
from app.models.audit import ToolCallAudit
from app.models.connection import OAuthConnection
from app.models.project import Project, ProjectMember
from app.models.user import User

NOW = dt.datetime(2026, 9, 24, 12, 0, 0)


# ── Pure helpers ─────────────────────────────────────────────────────────


def test_budgets_and_providers_track_the_catalog():
    keys = {c.key for c in rl.CATALOG}
    assert set(cu.BUDGETS) == keys
    for prov, conns in cu.PROVIDER_CONNECTORS.items():
        assert set(conns) <= keys, prov
    assert set(cu._PLATFORM_ALIASES.values()) <= keys
    for spec in cu.BUDGETS.values():
        if isinstance(spec, tuple):
            for b in spec:
                assert b.limit > 0 and b.window_s % 60 == 0


@pytest.mark.parametrize(
    ("tool", "platform", "action", "expected"),
    [
        ("analytics_read", "ga4", "run_report", "ga4"),
        ("analytics_read", "adobe_analytics", "get_metrics", "adobe_analytics"),
        ("marketing_read", "google", "list_accounts", "google_ads"),
        ("marketing_read", "meta", "list_accounts", "meta_ads"),
        ("warehouse_query", "bigquery", "run_query", "bigquery"),
        ("tagmanager_read", None, "list_tags", "gtm"),
        ("tagmanager_read", None, "list_rules", "adobe_launch"),
        ("tagmanager_read", "gtm", "list_rules", "adobe_launch"),
        ("seo_read", None, "list_sites", "search_console"),
        ("seo_read", None, "bing_list_sites", "bing_webmaster"),
        ("meta_get_insights", None, None, "meta_ads"),
        ("analytics_read", None, "run_report", None),  # platform missing -> errored locally
        ("run_script", None, None, None),
        ("dashboard_card_upsert", None, None, None),
        ("marketing_read", "unknown_platform", "x", None),
    ],
)
def test_resolve_connector(tool, platform, action, expected):
    assert cu.resolve_connector(tool, platform, action) == expected


def test_peak_in_window_uses_fixed_utc_windows():
    base = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
    minutes = {base: 3, base + 60: 4, base + 86400: 5}
    assert cu.peak_in_window(minutes, 60) == 5
    assert cu.peak_in_window(minutes, 3600) == 7
    assert cu.peak_in_window(minutes, 86400) == 7
    assert cu.peak_in_window({}, 60) == 0


def test_headroom_without_calls_is_full():
    v = cu.headroom_for("gtm", None)
    assert v["calls"] == 0 and v["metered"] is True
    assert v["headroom_pct"] == 100 and v["peak"] == 0
    assert v["limit_label"] == "15 / min"


def test_headroom_picks_the_most_utilised_window():
    base = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
    # 12 calls in one minute: 12/15 per-minute (80%) beats 12/10,000 per day.
    usage = cu.ConnectorUsage(calls=12, minutes={base: 12})
    v = cu.headroom_for("gtm", usage)
    assert v["window"] == "minute" and v["peak"] == 12
    assert v["headroom_pct"] == 20 and v["level"] == "warn"
    assert v["limit_source"] == "25 req / 100 s"


def test_headroom_floors_so_real_usage_is_never_100():
    usage = cu.ConnectorUsage(calls=1, minutes={0: 1})
    v = cu.headroom_for("google_ads", usage)  # 1 of 15,000 / day
    assert v["headroom_pct"] == 99 and v["window"] == "day"
    assert v["limit_label"] == "15,000 / day"


def test_headroom_saturates_at_zero():
    usage = cu.ConnectorUsage(calls=500, minutes={0: 500})
    v = cu.headroom_for("mixpanel", usage)  # 500 of 60 / min
    assert v["headroom_pct"] == 0 and v["used_pct"] == 100 and v["level"] == "bad"


def test_unmetered_connectors_report_a_reason():
    v = cu.headroom_for("ga4", cu.ConnectorUsage(calls=7))
    assert v == {"calls": 7, "days": 30, "metered": False, "reason": "quota is token-based"}


def test_relative_time():
    assert cu.relative_time(None, NOW) is None
    assert cu.relative_time(NOW - dt.timedelta(seconds=20), NOW) == "just now"
    assert cu.relative_time(NOW - dt.timedelta(minutes=5), NOW) == "5m ago"
    assert cu.relative_time(NOW - dt.timedelta(hours=2, minutes=5), NOW) == "2h ago"
    assert cu.relative_time(NOW - dt.timedelta(days=3), NOW) == "3d ago"
    assert cu.relative_time(dt.datetime(2026, 1, 5), NOW) == "Jan 5, 2026"


def test_combine_sums_google_services():
    t = NOW - dt.timedelta(hours=1)
    usage = {
        "ga4": cu.ConnectorUsage(calls=3, last_used=NOW - dt.timedelta(hours=5)),
        "gtm": cu.ConnectorUsage(calls=4, last_used=t),
        "meta_ads": cu.ConnectorUsage(calls=9, last_used=NOW),
    }
    g = cu.combine(usage, cu.PROVIDER_CONNECTORS["google"])
    assert g.calls == 7 and g.last_used == t


# ── DB-backed aggregation ────────────────────────────────────────────────


def _call(user_id, project_id, tool, created_at, platform=None, arguments=None, status="success"):
    return ToolCallAudit(
        user_id=user_id,
        project_id=project_id,
        tool_name=tool,
        platform=platform,
        status=status,
        arguments=arguments,
        created_at=created_at,
    )


@pytest.fixture
async def usage_ctx(db_engine, db_session_factory, monkeypatch):
    monkeypatch.setattr(app_state, "db_session_factory", db_session_factory)
    now = cu._utcnow()
    async with db_session_factory() as s:
        user = User(email=f"u-{uuid.uuid4().hex[:8]}@example.com", display_name="Usage User")
        other = User(email=f"o-{uuid.uuid4().hex[:8]}@example.com", display_name="Other User")
        s.add_all([user, other])
        await s.flush()
        project = Project(name="Usage Co", slug=f"usage-{uuid.uuid4().hex[:8]}", owner_id=user.id)
        elsewhere = Project(name="Elsewhere", slug=f"else-{uuid.uuid4().hex[:8]}", owner_id=user.id)
        s.add_all([project, elsewhere])
        await s.flush()
        s.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner", is_active=True))
        s.add_all(
            [
                OAuthConnection(
                    user_id=user.id,
                    project_id=project.id,
                    provider="google",
                    google_email="g@example.com",
                    access_token_encrypted="x",
                    refresh_token_encrypted="x",
                    scopes=[
                        "https://www.googleapis.com/auth/analytics.readonly",
                        "https://www.googleapis.com/auth/tagmanager.readonly",
                    ],
                ),
                OAuthConnection(
                    user_id=user.id,
                    project_id=project.id,
                    provider="meta",
                    google_email="meta@example.com",
                    access_token_encrypted="x",
                    refresh_token_encrypted="x",
                    scopes=["ads_read", "ads_management", "business_management"],
                ),
            ]
        )
        uid, pid = user.id, project.id
        minute = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=10)
        rows = [
            # GTM: 12 calls in one minute (two hours ago) + 1 old call.
            *[
                _call(
                    uid,
                    pid,
                    "tagmanager_read",
                    minute - dt.timedelta(hours=2) + dt.timedelta(seconds=i),
                    None,
                    {"action": "list_tags"},
                )
                for i in range(12)
            ],
            _call(uid, pid, "tagmanager_read", now - dt.timedelta(days=45), None, {"action": "list_tags"}),
            # GA4 via params.platform, 2 calls; most recent 2h ago.
            _call(
                uid,
                pid,
                "analytics_read",
                now - dt.timedelta(hours=2),
                None,
                {"action": "run_report", "params": {"platform": "ga4"}},
            ),
            _call(
                uid,
                pid,
                "analytics_read",
                now - dt.timedelta(hours=3),
                None,
                {"action": "run_report", "params": {"platform": "ga4"}},
            ),
            # Excluded: describe (local), denied (blocked), unattributable tool.
            _call(
                uid, pid, "analytics_read", now, None, {"action": "describe", "params": {"platform": "ga4"}}
            ),
            _call(
                uid,
                pid,
                "analytics_read",
                now,
                None,
                {"action": "run_report", "params": {"platform": "ga4"}},
                "denied",
            ),
            _call(uid, pid, "run_script", now, None, {"code": "1"}),
            # Meta via platform column, by ANOTHER user in the same project.
            _call(
                other.id,
                pid,
                "marketing_read",
                now - dt.timedelta(minutes=30),
                "meta",
                {"action": "list_accounts"},
            ),
            # Another project — never counted.
            _call(
                uid,
                elsewhere.id,
                "analytics_read",
                now,
                None,
                {"action": "run_report", "params": {"platform": "ga4"}},
            ),
        ]
        s.add_all(rows)
        await s.commit()
    return SimpleNamespace(uid=uid, other_uid=other.id, pid=pid, slug=project.slug, now=now)


@pytest.mark.asyncio
async def test_usage_summary_counts_30d_and_last_used(usage_ctx, db_session_factory):
    async with db_session_factory() as db:
        per_user = await cu.usage_summary(db, usage_ctx.pid, usage_ctx.uid)
        whole_project = await cu.usage_summary(db, usage_ctx.pid)
    assert per_user["gtm"].calls == 12  # the 45-day-old call is outside the window…
    assert per_user["gtm"].last_used is not None
    assert per_user["ga4"].calls == 2  # describe + denied excluded
    assert "meta_ads" not in per_user  # other user's call
    assert whole_project["meta_ads"].calls == 1
    assert set(whole_project) == {"gtm", "ga4", "meta_ads"}


@pytest.mark.asyncio
async def test_usage_by_minute_finds_the_busiest_minute(usage_ctx, db_session_factory):
    async with db_session_factory() as db:
        usage = await cu.usage_by_minute(db, usage_ctx.pid)
    assert usage["gtm"].calls == 12
    assert max(usage["gtm"].minutes.values()) == 12
    v = cu.headroom_for("gtm", usage["gtm"])
    assert v["peak"] == 12 and v["headroom_pct"] == 20
    assert usage["meta_ads"].calls == 1  # project-wide, all members


# ── Page rendering ───────────────────────────────────────────────────────


def _patch_auth(monkeypatch, uid):
    import app.api.google_oauth_routes as gor

    async def fake_ctx(request):
        return SimpleNamespace(user_id=str(uid), email="u@example.com")

    monkeypatch.setattr(gor, "_resolve_user_ctx", fake_ctx)


@pytest.mark.asyncio
async def test_connections_page_renders_real_usage(usage_ctx, monkeypatch):
    _patch_auth(monkeypatch, usage_ctx.uid)
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/settings/connections", follow_redirects=False)
    assert res.status_code == 200, res.text[:500]
    html = res.text
    # Google row: GTM (12) + GA4 (2) this user made, last one 2h ago.
    assert "<b>14</b> calls · 30d" in html
    assert "last used 2h ago" in html
    # Meta row: only another member used Meta, so this user's row shows none.
    assert "No calls in 30 days" in html
    # Scope pills from the real granted scopes: Google read-only, Meta has ads_management.
    assert html.count('cx-scope is-write">READ + WRITE') == 1


@pytest.mark.asyncio
async def test_api_limits_tab_renders_calls_and_headroom(usage_ctx, monkeypatch):
    _patch_auth(monkeypatch, usage_ctx.uid)
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get(f"/project/{usage_ctx.slug}/settings", follow_redirects=False)
    assert res.status_code == 200, res.text[:500]
    html = res.text
    # GTM: 12 calls, busiest minute 12 of 15/min → 20% headroom.
    assert '12<span class="rl-row-unit"> calls · 30d</span>' in html
    assert "20% headroom · busiest minute 12 of 15 / min" in html
    assert 'aria-valuenow="20"' in html
    # GA4's quota is token-based → no bar, an honest note instead.
    assert "Headroom n/a · quota is token-based" in html
