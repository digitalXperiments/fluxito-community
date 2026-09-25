# Fluxito Community — Project Instructions

This is the public community edition. It is a stable snapshot that receives
**security fixes only**. Do not add product features here (tracking plans,
dashboards, automations, agents, memory, avatars): they belong to the private
Fluxito Cloud repo. `tests/test_community_boundary.py` fails if removed
features come back.

## Committing

Do not commit after every small change. Commit only when explicitly asked, in
well-scoped commits with good messages.

## Before any `git push`: `tox` must be green (HARD GATE)

```bash
tox            # runs: lint, typecheck, test  (in that order)
```

- **lint** — `ruff check app tests` and `ruff format --check app tests` (pinned `ruff==0.8.4`).
- **typecheck** — `mypy` on the pinned critical modules.
- **test** — `pytest` (needs Postgres + Redis running locally).

## Releasing

Every push to `main` cuts a release (`vX.Y.Z`, track in `VERSION`) and publishes
the `fluxito-community*` images. Release notes are read verbatim from the
matching `CHANGELOG.md` section, so before pushing to `main`, rename
`## [Unreleased]` to `## [<next>] — <YYYY-MM-DD>` and write human-readable
entries. Put `[skip release]` in the commit message to push without a release.
