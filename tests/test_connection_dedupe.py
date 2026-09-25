"""Unit tests for dedupe_connections — one card per (provider, google_email)."""

from app.api.project_routes import dedupe_connections


def _row(id, provider, email, status):
    return {"id": id, "provider": provider, "google_email": email, "status": status}


def test_collapses_same_account_to_one_card():
    rows = [
        _row("1", "google", "jeff@x.com", "active"),
        _row("2", "google", "jeff@x.com", "active"),
    ]
    out = dedupe_connections(rows)
    assert len(out) == 1
    assert out[0]["connected_by_count"] == 2
    assert out[0]["status"] == "active"


def test_active_beats_disconnected():
    rows = [
        _row("1", "google", "jeff@x.com", "disconnected"),
        _row("2", "google", "jeff@x.com", "active"),
    ]
    out = dedupe_connections(rows)
    assert len(out) == 1
    assert out[0]["status"] == "active"


def test_distinct_accounts_kept_separate():
    rows = [
        _row("1", "google", "a@x.com", "active"),
        _row("2", "google", "b@x.com", "active"),
    ]
    out = dedupe_connections(rows)
    assert len(out) == 2
