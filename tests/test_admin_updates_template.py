from pathlib import Path

ADMIN_TEMPLATE = Path("app/templates/admin.html")


def _updates_source() -> str:
    source = ADMIN_TEMPLATE.read_text()
    return source[source.index('data-panel="updates"') :]


def test_updates_use_in_page_confirmation_instead_of_browser_popups():
    source = _updates_source()

    assert "confirm('Update now?" not in source
    assert "alert('Could not start update:" not in source
    assert 'id="upd-confirm"' in source
    assert 'id="upd-confirm-start"' in source
    assert 'id="upd-confirm-cancel"' in source


def test_updates_include_accessible_stage_progress_and_recovery_actions():
    source = _updates_source()

    assert 'id="upd-workflow"' in source
    assert 'role="progressbar"' in source
    assert 'aria-live="polite"' in source
    for stage in ("Download update", "Restart Fluxito", "Verify health", "Complete"):
        assert stage in source
    assert 'id="upd-troubleshooting"' in source
    assert 'id="upd-copy-diagnostics"' in source
    assert 'id="upd-retry"' in source
    assert "<details" in source
    assert "Technical details" in source


def test_updates_centralize_stage_and_failure_mappings():
    source = _updates_source()

    assert "var UPDATE_STAGES" in source
    assert "var FAILURE_GUIDANCE" in source
    assert "function renderJob" in source
    assert "function renderFailure" in source


def test_updates_have_manual_check_button_wired_to_force_endpoint():
    source = _updates_source()

    assert 'id="upd-check"' in source  # the button
    assert 'id="upd-checked"' in source  # "last checked" status line
    assert "/api/updates/check" in source  # POSTs to the force endpoint
    assert "function checkForUpdates" in source  # handler defined
