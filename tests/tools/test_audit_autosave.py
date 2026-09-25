"""run_audit auto-save: audits run in chat must land on the Audit page."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.app_state as state
from app.tools import audit_autosave
from app.tools.audit_autosave import audit_type_for, autosave_audit_result, findings_from_result


def test_audit_type_mapping():
    assert audit_type_for("gtm_audit_container") == "tag_audit"
    assert audit_type_for("adobe_launch_audit_property") == "tag_audit"
    assert audit_type_for("adobe_audit_report_suite") == "data_quality"
    assert audit_type_for("ga4_audit_ecommerce") == "data_quality"
    assert audit_type_for("warehouse_audit_dataset") == "warehouse"
    assert audit_type_for("seo_sitemap_health") == "seo"
    # Metadata / persistence actions are never saved as runs.
    assert audit_type_for("gtm_explain_tag") is None
    assert audit_type_for("tag_list_platforms") is None
    assert audit_type_for("save_audit_result") is None


def test_findings_normalize_gtm_issue_shape():
    result = {
        "score": 0,
        "issues": [
            {"severity": "warning", "category": "triggers", "issue": "Trigger 'X' has no tags using it"},
            {"severity": "high", "issue": "Duplicate GA4 config tag"},
            {"severity": "warning"},  # no message → dropped
        ],
        "recommendations": ["Remove orphaned triggers"],
    }
    findings = findings_from_result(result)
    assert [f["severity"] for f in findings] == ["warning", "critical", "info"]
    assert findings[0]["message"] == "Trigger 'X' has no tags using it"
    assert findings[0]["rule_id"] == "triggers"
    assert findings[2]["message"] == "Remove orphaned triggers"


@pytest.fixture
def ctx(monkeypatch):
    user = SimpleNamespace(user_id=str(uuid.uuid4()))
    proj = SimpleNamespace(project_id=str(uuid.uuid4()), project_slug="p")
    monkeypatch.setattr(audit_autosave, "get_current_user", lambda: user)
    tok = state.current_project_ctx.set(proj)
    audit_autosave._recent.clear()
    saved: list[dict] = []

    async def fake_persist(**kw):
        saved.append(kw)
        return {"audit_run_id": "run-1", "view_url": "https://x/audits/run/run-1"}

    monkeypatch.setattr("app.tools.save_audit_result_tools.persist_audit_run", fake_persist)
    yield saved
    state.current_project_ctx.reset(tok)


async def test_autosave_persists_and_annotates(ctx):
    result = {"score": 42, "issues": [{"severity": "warning", "issue": "orphaned trigger"}]}
    out = await autosave_audit_result("gtm_audit_container", {"container_id": "123"}, result, 50)
    assert out["saved_to_fluxito"]["audit_run_id"] == "run-1"
    assert len(ctx) == 1
    assert ctx[0]["audit_type"] == "tag_audit"
    assert ctx[0]["score"] == 42
    assert ctx[0]["triggered_by"] == "claude"
    assert ctx[0]["title"] == "GTM Audit Container — 123"

    # A duplicate call right after is not saved twice.
    await autosave_audit_result("gtm_audit_container", {"container_id": "123"}, result, 50)
    assert len(ctx) == 1


async def test_autosave_skips_errors_and_non_audits(ctx):
    err = {"error": True, "message": "boom"}
    assert await autosave_audit_result("gtm_audit_container", {}, err, 1) is err
    meta = {"issues": [{"issue": "x"}]}
    assert await autosave_audit_result("gtm_explain_tag", {}, meta, 1) is meta
    empty = {"tags": []}
    assert await autosave_audit_result("gtm_audit_container", {}, empty, 1) is empty
    assert ctx == []
