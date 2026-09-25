import importlib.util
import pathlib

# Load updater/server.py as a module without it being a package.
_SPEC = importlib.util.spec_from_file_location(
    "updater_server", pathlib.Path(__file__).resolve().parent.parent / "updater" / "server.py"
)
updater = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(updater)


def test_valid_version_accepts_semver():
    assert updater.is_valid_version("1.0.5") is True
    assert updater.is_valid_version("12.34.56") is True


def test_valid_version_rejects_injection():
    assert updater.is_valid_version("1.0.5; rm -rf /") is False
    assert updater.is_valid_version("latest") is False
    assert updater.is_valid_version("../../etc") is False
    assert updater.is_valid_version("") is False


def test_upsert_env_var_adds_when_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("POSTGRES_PASSWORD=secret\n")
    updater.upsert_env_var(str(env), "FLUXITO_VERSION", "1.0.5")
    text = env.read_text()
    assert "FLUXITO_VERSION=1.0.5" in text
    assert "POSTGRES_PASSWORD=secret" in text  # other vars untouched


def test_upsert_env_var_replaces_existing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FLUXITO_VERSION=1.0.4\nPOSTGRES_PASSWORD=secret\n")
    updater.upsert_env_var(str(env), "FLUXITO_VERSION", "1.0.5")
    text = env.read_text()
    assert "FLUXITO_VERSION=1.0.5" in text
    assert "FLUXITO_VERSION=1.0.4" not in text
    assert text.count("FLUXITO_VERSION=") == 1


def test_valid_version_rejects_newline_injection():
    assert updater.is_valid_version("1.0.4\nPOSTGRES_PASSWORD=hacked") is False
