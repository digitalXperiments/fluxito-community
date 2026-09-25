"""Single runtime source of the running Fluxito version.

Released images bake the exact version in via the ``APP_VERSION`` build-arg
(set by the release workflow). Source/build-from-source installs derive the
version from the newest released ``## [x.y.z]`` heading in ``CHANGELOG.md``.
The ``+local`` fallback is only used when CHANGELOG is unreadable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_CHANGELOG_FILE = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

_SEMVER_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]")


def read_track() -> str:
    """Return the MAJOR.MINOR track from the repo-root VERSION file."""
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0"
    except OSError:
        return "0.0"


def _changelog_version() -> str | None:
    """Return the newest released version from CHANGELOG.md, or None on failure.

    Scans line-by-line for the first ``## [MAJOR.MINOR.PATCH]`` heading,
    correctly skipping ``## [Unreleased]`` which contains no semver.
    """
    try:
        with _CHANGELOG_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                m = _SEMVER_HEADING.match(line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def get_version() -> str:
    """Resolve the running full version.

    Precedence:
    1. Baked ``APP_VERSION`` env (released GHCR images).
    2. Newest ``## [x.y.z]`` heading from ``CHANGELOG.md`` (source builds).
    3. ``<track>.0+local`` last-resort fallback (CHANGELOG unreadable).
    """
    baked = os.environ.get("APP_VERSION", "").strip()
    if baked:
        return baked
    changelog = _changelog_version()
    if changelog is not None:
        return changelog
    return f"{read_track()}.0+local"


__version__ = get_version()
