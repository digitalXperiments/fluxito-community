"""Manual drift check for the connector rate-limit catalog.

The published limits in :mod:`app.connectors.rate_limits` drift over time, so
this is a low-effort way to re-verify them by hand whenever you feel like it —
no schedule, no GitHub, no cloud agent. Two layers:

1. DETERMINISTIC — :func:`audit` flags every connector whose ``reviewed`` date is
   older than ``max_age_days`` (default 180 ≈ 6 months). Pure stdlib. The CLI
   exits 1 when anything is stale, so it also slots into cron/CI if you ever want
   that. Run it anywhere the app imports, including the production host::

       python -m app.connectors.rate_limits_drift

2. SEMANTIC — :data:`DRIFT_PROMPT` is a self-contained prompt that re-fetches each
   connector's ``docs_url`` and diffs the live numbers against the catalog. Feed
   it to Claude Code when you want a full re-check like the one that seeded the
   catalog::

       claude -p "$(python -m app.connectors.rate_limits_drift --prompt)"

CLI::

    python -m app.connectors.rate_limits_drift                 # report, exit 1 if stale
    python -m app.connectors.rate_limits_drift --json          # machine-readable
    python -m app.connectors.rate_limits_drift --max-age-days 90
    python -m app.connectors.rate_limits_drift --prompt        # print the semantic prompt
"""

from __future__ import annotations

import argparse
import datetime as dt
import json

from app.connectors import rate_limits as rl

DRIFT_PROMPT = """\
Re-verify Fluxito's connector API rate-limit catalog against the providers' live \
documentation, and report any drift. Do NOT edit code — this is a read-only audit.

The catalog is `app/connectors/rate_limits.py`. Each `Connector` in `CATALOG` has:
`key`, `name`, `docs_url`, `limits` (a tuple of `Limit(name, value, window, scope, note)`),
`error_behavior`, `headers`, a `reviewed` date, and a `confidence` (high/medium/low).

For every connector in `CATALOG`:
1. Read its recorded `limits`, `error_behavior` and `reviewed` date from the file.
2. Fetch its `docs_url`. If the page moved, is geo-blocked, or doesn't state numbers,
   web-search the provider's OFFICIAL developer docs (prefer first-party over blogs).
3. Compare the provider's CURRENT official numbers against what the catalog records.

Produce a concise report in three groups:
- CHANGED — limits/error-behavior/tiers that no longer match. Give old -> new, which
  `Limit` row it affects, and the official source URL.
- STALE — `reviewed` date older than 6 months (re-verify even if unchanged).
- UNVERIFIED — couldn't confirm (doc unreachable, geo-blocked, or no published number;
  TikTok, Apple Search Ads and Adobe Launch are expected to be hard — don't treat a
  missing number as drift).

If there is at least one CHANGED item and `gh` is available and authenticated, open a
GitHub issue titled `Rate-limit catalog drift: <YYYY-MM>` whose body is a checklist of
edits needed in `app/connectors/rate_limits.py` (old -> new, source URL, and a reminder to
bump that connector's `reviewed` date). Otherwise just print the report.

If nothing changed, say "no drift" and list which connectors are now stale.
"""


def audit(max_age_days: int = 180, today: dt.date | None = None) -> list[dict]:
    """Return one row per connector with its review age and staleness flag.

    A row is ``stale`` if its ``reviewed`` date is older than ``max_age_days``,
    or if the date can't be parsed (which itself warrants a look).
    """
    today = today or dt.date.today()
    rows: list[dict] = []
    for c in rl.CATALOG:
        try:
            reviewed = dt.date.fromisoformat(c.reviewed)
            age: int | None = (today - reviewed).days
            stale = age > max_age_days
        except ValueError:
            age = None
            stale = True
        rows.append(
            {
                "key": c.key,
                "name": c.name,
                "docs_url": c.docs_url,
                "reviewed": c.reviewed,
                "age_days": age,
                "confidence": c.confidence,
                "stale": stale,
            }
        )
    return rows


def _format_report(rows: list[dict], max_age_days: int) -> str:
    stale = [r for r in rows if r["stale"]]
    lines = [
        f"Connector rate-limit catalog: {len(rows)} connectors, "
        f"{len(stale)} due for re-verification (> {max_age_days} days).",
        "",
    ]
    if stale:
        lines.append("STALE — re-verify against the official docs and bump `reviewed`:")
        for r in sorted(stale, key=lambda r: (r["age_days"] is None, -(r["age_days"] or 0))):
            age = "unparseable date" if r["age_days"] is None else f"{r['age_days']}d old"
            lines.append(f"  • {r['name']:<24} reviewed {r['reviewed']} ({age})  {r['docs_url']}")
        lines.append("")
        lines.append("Next, run the semantic check —")
        lines.append('  claude -p "$(python -m app.connectors.rate_limits_drift --prompt)"')
    else:
        lines.append("All entries are fresh. For a live numbers check, run the semantic pass:")
        lines.append('  claude -p "$(python -m app.connectors.rate_limits_drift --prompt)"')
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.connectors.rate_limits_drift",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--max-age-days", type=int, default=180, help="Staleness threshold (default: 180)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--prompt", action="store_true", help="Print the semantic re-verification prompt and exit"
    )
    args = parser.parse_args(argv)

    if args.prompt:
        print(DRIFT_PROMPT)
        return 0

    rows = audit(args.max_age_days)
    stale = [r for r in rows if r["stale"]]

    if args.json:
        print(
            json.dumps(
                {"max_age_days": args.max_age_days, "stale_count": len(stale), "connectors": rows}, indent=2
            )
        )
    else:
        print(_format_report(rows, args.max_age_days))

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
