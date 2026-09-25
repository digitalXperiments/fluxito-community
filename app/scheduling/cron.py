"""
Cadence → cron expression.

Scheduled reports support four cadences at the model layer:

  * ``daily``       — fires once per day at a user-chosen hour:minute
  * ``weekly``      — fires once per week on a chosen weekday at hour:minute
  * ``monthly``     — fires on a chosen day-of-month at hour:minute
  * ``custom_cron`` — the user supplied a raw cron expression directly

Internally APScheduler reads *only* the cron expression (stored on
``ReportSchedule.cron_expression``) — so the save path normalises every
preset cadence into a cron string at write time. That way:

  1. The worker path has exactly one field to trust.
  2. Preset cadences and custom cron live on the same plumbing; we only
     decide between them at the UI layer.
  3. Debugging is easier — "what time does this run?" always has a
     single, canonical answer you can paste into a cron explainer.

Cron flavour: 5-field POSIX-style
  ``minute hour day-of-month month day-of-week``
with ``*`` as the wildcard. Day-of-week uses Sun..Sat as 0..6 (cron's
"traditional" numbering — this is what APScheduler's ``CronTrigger``
accepts via ``from_crontab``).

We also expose ``validate_cron_expression`` for the CRUD layer: it
returns (True, None) on success and (False, reason) on failure, without
raising, so the route can return a 400 with a friendly error.
"""

from __future__ import annotations

CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
CADENCE_MONTHLY = "monthly"
CADENCE_CUSTOM = "custom_cron"
VALID_CADENCES = {CADENCE_DAILY, CADENCE_WEEKLY, CADENCE_MONTHLY, CADENCE_CUSTOM}

# Weekday mapping — match cron's Sun..Sat = 0..6. The UI passes lowercase
# strings so users never have to know the numbers.
_WEEKDAY_NAMES = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}


class CronValidationError(ValueError):
    """Raised by ``cadence_to_cron`` when the inputs are malformed."""


def cadence_to_cron(
    cadence: str,
    *,
    hour: int = 9,
    minute: int = 0,
    weekday: str | int | None = None,
    day_of_month: int | None = None,
    custom_cron: str | None = None,
) -> str:
    """Convert a preset cadence into a 5-field cron expression.

    Args:
      cadence       — one of ``daily``, ``weekly``, ``monthly``,
                      ``custom_cron``.
      hour          — 0..23 (ignored for ``custom_cron``). Default 9.
      minute        — 0..59 (ignored for ``custom_cron``). Default 0.
      weekday       — required for ``weekly``. Accepts either a lowercase
                      name (``"mon"``, ``"monday"``) or an int 0..6
                      (Sun..Sat). Ignored for other cadences.
      day_of_month  — required for ``monthly``. 1..28 is safe for all
                      months; 29/30/31 will skip months without that day,
                      which APScheduler handles correctly.
      custom_cron   — required for ``custom_cron``. Must already be a
                      valid 5-field expression — we pass it through after
                      validation.

    Returns:
      5-field cron expression as a string.

    Raises:
      CronValidationError — bad cadence or out-of-range fields.
    """
    if cadence not in VALID_CADENCES:
        raise CronValidationError(
            f"unknown cadence '{cadence}'. expected one of: {', '.join(sorted(VALID_CADENCES))}"
        )

    if cadence == CADENCE_CUSTOM:
        if not custom_cron:
            raise CronValidationError("custom_cron cadence requires a non-empty custom_cron expression")
        ok, reason = validate_cron_expression(custom_cron)
        if not ok:
            raise CronValidationError(f"invalid custom cron: {reason}")
        return custom_cron.strip()

    _validate_time_of_day(hour, minute)

    if cadence == CADENCE_DAILY:
        return f"{minute} {hour} * * *"

    if cadence == CADENCE_WEEKLY:
        dow = _coerce_weekday(weekday)
        return f"{minute} {hour} * * {dow}"

    if cadence == CADENCE_MONTHLY:
        if day_of_month is None:
            raise CronValidationError("monthly cadence requires a day_of_month")
        if not isinstance(day_of_month, int) or not (1 <= day_of_month <= 31):
            raise CronValidationError("day_of_month must be an int in [1, 31]")
        return f"{minute} {hour} {day_of_month} * *"

    # Unreachable — all enum values handled above.
    raise CronValidationError(f"internal: unhandled cadence '{cadence}'")


def validate_cron_expression(expr: str) -> tuple[bool, str | None]:
    """Return (True, None) if ``expr`` is a 5-field cron expression.

    This is intentionally permissive — we rely on APScheduler's
    ``CronTrigger.from_crontab`` to be the final arbiter at job-add
    time. What we check here is the gross shape so the UI can show a
    useful error before the row is saved.
    """
    if not isinstance(expr, str):
        return False, "cron expression must be a string"
    stripped = expr.strip()
    if not stripped:
        return False, "cron expression is empty"
    parts = stripped.split()
    if len(parts) != 5:
        return False, f"expected 5 fields, got {len(parts)}"
    # We don't fully parse the fields — just reject characters that
    # APScheduler is guaranteed to reject so the user gets faster feedback.
    allowed = set("0123456789*/,-")
    for i, p in enumerate(parts):
        # Weekday and month fields can contain letters (JAN..DEC, SUN..SAT).
        if i in (3, 4):
            if any(
                c not in (allowed | set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")) for c in p
            ):
                return False, f"field {i + 1} has unexpected characters: {p!r}"
        else:
            if any(c not in allowed for c in p):
                return False, f"field {i + 1} has unexpected characters: {p!r}"
    return True, None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _validate_time_of_day(hour: int, minute: int) -> None:
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        raise CronValidationError(f"hour must be an int in [0, 23] (got {hour!r})")
    if not isinstance(minute, int) or not (0 <= minute <= 59):
        raise CronValidationError(f"minute must be an int in [0, 59] (got {minute!r})")


def _coerce_weekday(weekday: str | int | None) -> int:
    if weekday is None:
        raise CronValidationError("weekly cadence requires a weekday")
    if isinstance(weekday, int):
        if not (0 <= weekday <= 6):
            raise CronValidationError("weekday int must be 0..6 (Sun..Sat)")
        return weekday
    if isinstance(weekday, str):
        key = weekday.strip().lower()
        if key in _WEEKDAY_NAMES:
            return _WEEKDAY_NAMES[key]
        # Accept numeric strings too
        if key.isdigit():
            return _coerce_weekday(int(key))
        raise CronValidationError(
            f"unknown weekday '{weekday}'. expected one of: {', '.join(sorted(set(_WEEKDAY_NAMES.keys())))}"
        )
    raise CronValidationError(f"weekday must be a str or int (got {type(weekday).__name__})")
