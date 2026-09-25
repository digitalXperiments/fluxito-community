import importlib
import re

import app._version as version_mod


def _reload():
    return importlib.reload(version_mod)


def test_baked_app_version_takes_precedence(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "1.0.7")
    mod = _reload()
    assert mod.get_version() == "1.0.7"


def test_source_build_uses_changelog_version(monkeypatch):
    """With APP_VERSION unset and the real CHANGELOG present, get_version() returns
    the newest ## [x.y.z] heading — a clean semver, NOT ending in +local."""
    monkeypatch.delenv("APP_VERSION", raising=False)
    mod = _reload()
    v = mod.get_version()
    # Must match the changelog version exactly
    assert v == mod._changelog_version()
    # Must be a clean 3-part semver with no +local
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"Expected clean semver, got {v!r}"
    assert "+local" not in v


def test_blank_app_version_uses_changelog(monkeypatch):
    """A blank APP_VERSION is treated as unset; changelog version is used."""
    monkeypatch.setenv("APP_VERSION", "   ")
    mod = _reload()
    v = mod.get_version()
    # Should use changelog, not +local (assuming CHANGELOG is present)
    assert "+local" not in v


def test_fallback_to_local_when_changelog_missing(monkeypatch, tmp_path):
    """With APP_VERSION unset AND CHANGELOG unreadable, falls back to <track>.0+local."""
    import app._version as v

    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setattr(v, "_CHANGELOG_FILE", tmp_path / "nope" / "CHANGELOG.md")
    result = v.get_version()
    assert result.endswith("+local")
    assert result.count(".") == 2


def test_changelog_version_skips_unreleased_monkeypatch(monkeypatch, tmp_path):
    """Same as above but using monkeypatch for cleanliness."""
    import app._version as v

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\nsome notes\n\n## [2.3.4] — 2026-01-01\n\n### Added\n- stuff\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(v, "_CHANGELOG_FILE", changelog)
    assert v._changelog_version() == "2.3.4"


def test_changelog_version_returns_none_on_missing_file(monkeypatch, tmp_path):
    """_changelog_version() returns None if CHANGELOG.md doesn't exist."""
    import app._version as v

    monkeypatch.setattr(v, "_CHANGELOG_FILE", tmp_path / "nonexistent.md")
    assert v._changelog_version() is None


def test_read_track_missing_file_falls_back(monkeypatch, tmp_path):
    import app._version as v

    monkeypatch.setattr(v, "_VERSION_FILE", tmp_path / "nope" / "VERSION")
    assert v.read_track() == "0.0"


def test_module_version_matches_get_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "2.3.4")
    import app._version as v

    importlib.reload(v)
    assert v.__version__ == "2.3.4" == v.get_version()


def test_config_uses_runtime_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    import importlib

    import app._version as v
    import app.config as config

    importlib.reload(v)  # rebind get_version to read the new env
    importlib.reload(config)  # re-executes Settings body, re-imports _get_version from fresh v
    assert config.Settings.model_fields["MCP_SERVER_VERSION"].default == "9.9.9"
