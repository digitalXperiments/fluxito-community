"""
Email service for transactional emails.

Supports multiple backends:
  - **SMTP** — standard email via any SMTP provider (SES, SendGrid, etc.)
  - **Console** — logs emails to stdout (development default)

All email sending is async and fire-and-forget to avoid blocking request
handlers. Failed sends are logged but never raise.
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import app.app_state as app_state
from app.config import settings
from app.settings_service import get_runtime_setting

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


async def _email_config() -> dict:
    """Return DB-backed email settings with env/default fallback."""
    fallback = {
        "host": getattr(settings, "SMTP_HOST", ""),
        "port": int(getattr(settings, "SMTP_PORT", 587)),
        "username": getattr(settings, "SMTP_USERNAME", ""),
        "password": getattr(settings, "SMTP_PASSWORD", ""),
        "from_email": getattr(settings, "SMTP_FROM_EMAIL", "noreply@example.com"),
        "from_name": getattr(settings, "SMTP_FROM_NAME", ""),
    }
    session_factory = getattr(app_state, "db_session_factory", None)
    if session_factory is None:
        return fallback

    try:
        async with session_factory() as db:
            return {
                "host": await get_runtime_setting(db, "smtp_host"),
                "port": await get_runtime_setting(db, "smtp_port"),
                "username": await get_runtime_setting(db, "smtp_username"),
                "password": await get_runtime_setting(db, "smtp_password"),
                "from_email": await get_runtime_setting(db, "smtp_from_email"),
                "from_name": await get_runtime_setting(db, "smtp_from_name"),
            }
    except Exception:
        logger.exception("Failed to load DB email settings; using env/default fallback")
        return fallback


# ---------------------------------------------------------------------------
# Low-level send
# ---------------------------------------------------------------------------


async def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
):
    """
    Send an email. Uses SMTP if configured, otherwise logs to console.
    """
    from app.branding import brand as _brand

    cfg = await _email_config()
    from_email = cfg["from_email"] or "noreply@example.com"
    from_name = cfg["from_name"] or _brand()["name"]

    if not (cfg["host"] and from_email):
        logger.info(f"[EMAIL-DEV] To: {to_email} | Subject: {subject}\n{text_body or html_body[:500]}")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = to_email

        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        host = cfg["host"]
        port = int(cfg["port"] or 587)
        user = cfg["username"] or ""
        password = cfg["password"] or ""

        with smtplib.SMTP(host, port) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(from_email, [to_email], msg.as_string())

        logger.info(f"Email sent to {to_email}: {subject}")
    except Exception:
        logger.exception(f"Failed to send email to {to_email}")


# ---------------------------------------------------------------------------
# Project invite email
# ---------------------------------------------------------------------------


async def send_project_invite_email(
    to_email: str,
    project_name: str,
    project_slug: str,
    inviter_email: str,
    role: str,
    inviter_name: str | None = None,
    accept_url: str | None = None,
):
    """Send an invitation email for a project.

    ``accept_url`` is the invite's one-time accept link (``/invite/<token>``);
    without it the email links to the project page.

    ``inviter_name`` is the inviter's display name for the headline/subject
    ("Priya invited you to Acme Marketing"); when omitted it falls back to
    the local-part of ``inviter_email`` (e.g. "priya" from "priya@acme.co").
    """
    from app.branding import brand as _brand

    brand_name = _brand()["name"]

    base_url = getattr(settings, "APP_BASE_URL", "https://fluxito.ai")
    invite_url = accept_url or f"{base_url}/project/{project_slug}"

    inviter_display = inviter_name or inviter_email.split("@")[0]

    subject = f"{inviter_display} invited you to {project_name}"

    # Brand font stacks with system fallbacks — same as the morning-briefing
    # email in this file (Newsreader / Archivo / IBM Plex Mono).
    serif = "'Newsreader', Georgia, 'Times New Roman', serif"
    sans = "'Archivo', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
    mono = "'IBM Plex Mono', 'SF Mono', 'Courier New', monospace"
    inviter_initials = (inviter_display[:2] or "??").upper()
    project_initials = (project_name[:2] or "??").upper()
    role_label = role.title() if role else "Member"

    html_body = f"""
    <div style="font-family: {sans}; max-width: 520px; margin: 0 auto; padding: 32px 16px; background: #E8E1D3;">
      <div style="background: #F6F1E8; border: 1px solid #DDD3C2; border-radius: 14px; overflow: hidden;">
        <div style="padding: 14px 24px; background: #FFFDF8; border-bottom: 1px solid #EEE6D6; font-size: 12px; color: #8A857C;">
          <strong style="color: #39332A;">From:</strong> {brand_name} &lt;hello@fluxito.app&gt; &middot;
          <strong style="color: #39332A;">Subject:</strong> {subject}
        </div>
        <div style="padding: 32px; text-align: center;">
          <div style="font-family: {serif}; font-size: 22px; font-weight: 600; color: #201B14; margin-bottom: 22px;">
            {brand_name}<span style="color: #C4703A;">&nbsp;&#9679;</span>
          </div>
          <div style="margin-bottom: 20px;">
            <span style="display: inline-block; width: 46px; height: 46px; border-radius: 999px; background: #C4703A;
                         color: #FFF9EF; font-family: {sans}; font-size: 16px; font-weight: 700; line-height: 46px;">
              {inviter_initials}
            </span>
            <span style="font-family: {mono}; color: #A89F8D; padding: 0 12px;">&rarr;</span>
            <span style="display: inline-block; width: 46px; height: 46px; border-radius: 11px; background: #201B14;
                         color: #F6F1E8; font-family: {mono}; font-size: 14px; font-weight: 600; line-height: 46px;">
              {project_initials}
            </span>
          </div>
          <div style="font-family: {serif}; font-size: 26px; font-weight: 500; line-height: 1.15; color: #201B14; margin-bottom: 8px;">
            {inviter_display} invited you to <em style="color: #A85A2B;">{project_name}.</em>
          </div>
          <p style="font-size: 14px; color: #57503F; line-height: 1.6; max-width: 360px; margin: 0 auto 22px;">
            You'll join as a{'n' if role_label[:1].lower() in 'aeiou' else ''} <strong>{role_label}</strong> &mdash;
            and meet Flux, the analytics teammate that already knows this project's stack.
          </p>
          <a href="{invite_url}"
             style="display: inline-block; background: #C4703A; color: #FFF9EF; font-size: 14.5px;
                    font-weight: 600; padding: 13px 28px; border-radius: 9px; text-decoration: none;">
            Accept invite
          </a>
          <p style="font-family: {mono}; font-size: 10.5px; color: #A89F8D; margin: 18px 0 0;">
            This invite expires in 7 days.
          </p>
        </div>
        <div style="padding: 14px 32px; border-top: 1px solid #EEE6D6; font-family: {mono};
                    font-size: 10.5px; color: #A89F8D; text-align: center;">
          {brand_name} &middot; open-source marketing analytics ops &middot;
          <a href="https://fluxito.app" style="color: #8A857C; text-decoration: underline;">fluxito.app</a>
        </div>
      </div>
    </div>
    """

    text_body = (
        f"{inviter_display} has invited you to join '{project_name}' on {brand_name} as a {role}.\n\n"
        f"Open the project: {invite_url}\n\n"
        "This invite expires in 7 days."
    )

    await send_email(to_email, subject, html_body, text_body)


# ---------------------------------------------------------------------------
# Morning-briefing digest email
# ---------------------------------------------------------------------------


def _briefing_severity_style(severity: str) -> tuple[str, str]:
    """Return (dot, text) hex pair for a briefing finding, matching the
    Ledger tokens (--bad/--warn/--good in app/static/css/app.css) as literal
    hex — email clients don't resolve CSS custom properties. The dot color
    (first element) renders as a plain 7x7px circle bullet; the text color
    (second element) is currently unused by the finding row itself but kept
    for callers that still want a label/pill treatment elsewhere."""
    return {
        "urgent": ("#B4452F", "#FFF6F0"),
        "watch": ("#C4903A", "#FFF9EF"),
        "good": ("#3E8A5F", "#F2FBF4"),
    }.get(severity, ("#8A857C", "#FFFDF8"))


async def send_morning_briefing_email(
    to_email: str,
    user_name: str,
    project_name: str,
    findings: list[dict],
    briefing_url: str,
    checked_summary: str | None = None,
):
    """Send Flux's daily morning-briefing digest email.

    ``findings`` must already be a built list of dicts shaped like the Home
    briefing feed (see ``briefing_findings`` built by ``home()`` in
    app/api/google_oauth_routes.py) — each with at least ``severity``
    ("urgent" / "watch" / "good") and ``title``, and optionally ``body``. This
    function takes the list as an argument rather than re-deriving it from the
    DB/audit tables itself, so it has no dependency on request/session state
    and can be called from a scheduler, a CLI, or a route handler alike.

    ``checked_summary`` is an optional one-line overnight-scan summary (e.g.
    "I checked 12 data sources overnight"); a generic fallback is used when
    omitted.
    """
    from app.branding import brand as _brand

    brand_name = _brand()["name"]

    serif = "'Newsreader', Georgia, 'Times New Roman', serif"
    sans = "'Archivo', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
    mono = "'IBM Plex Mono', 'SF Mono', 'Courier New', monospace"

    urgent_count = sum(1 for f in findings if f.get("severity") == "urgent")
    watch_count = sum(1 for f in findings if f.get("severity") == "watch")
    needs_attention = urgent_count + watch_count

    now = datetime.now()
    weekday = now.strftime("%a")
    date_display = now.strftime("%a %b %-d").upper()
    time_display = now.strftime("%H:%M")
    project_display = (project_name or "").upper()
    time_of_day = "Morning" if now.hour < 12 else "Afternoon" if now.hour < 18 else "Evening"

    subject_bits = []
    if urgent_count:
        subject_bits.append(f"{urgent_count} urgent")
    if watch_count:
        subject_bits.append(f"{watch_count} to watch")
    subject_tail = ", ".join(subject_bits) if subject_bits else "all clear"
    subject = f"☕ {weekday} briefing — {subject_tail}"

    first_name = (user_name or "there").split(" ")[0]
    if needs_attention == 1:
        headline = f"{time_of_day}, {first_name}. One thing needs you."
    elif needs_attention > 1:
        headline = f"{time_of_day}, {first_name}. {needs_attention} things need you."
    else:
        headline = f"{time_of_day}, {first_name}. Nothing urgent today."

    summary_line = (
        checked_summary
        or "I checked your connected data sources overnight — the full detail is on your home screen."
    )

    finding_rows = ""
    for i, f in enumerate(findings):
        dot_color, _fg = _briefing_severity_style(f.get("severity", ""))
        title = f.get("title", "")
        body = f.get("body", "")
        border = "" if i == len(findings) - 1 else "border-bottom:1px solid #EEE6D6;"
        finding_rows += f"""
          <div style="display:flex; gap:11px; align-items:flex-start; padding:14px 16px; {border}">
            <span style="width:7px; height:7px; border-radius:999px; background:{dot_color};
                         margin-top:6px; flex-shrink:0; display:inline-block;"></span>
            <div style="font-size:13px; color:#39332A; line-height:1.55;">
              <strong>{title}</strong>{" &mdash; " + body if body else ""}
            </div>
          </div>"""

    html_body = f"""
    <div style="font-family: {sans}; max-width: 520px; margin: 0 auto; padding: 32px 16px; background: #E8E1D3;">
      <div style="background: #F6F1E8; border: 1px solid #DDD3C2; border-radius: 14px; overflow: hidden;">
        <div style="padding: 14px 24px; background: #FFFDF8; border-bottom: 1px solid #EEE6D6; font-size: 12px; color: #8A857C;">
          <strong style="color: #39332A;">From:</strong> Flux &lt;briefings@{(brand_name or "fluxito").lower()}.app&gt; &middot;
          <strong style="color: #39332A;">Subject:</strong> {subject}
        </div>
        <div style="padding: 28px 32px;">
          <div style="display:flex; align-items:center; gap:10px; margin-bottom:20px;">
            <span style="width:32px; height:32px; border-radius:8px; background:#201B14; color:#F6F1E8; display:inline-flex;
                         align-items:center; justify-content:center; font-family:{serif}; font-style:italic; font-size:16px;">F</span>
            <div>
              <div style="font-size:13.5px; font-weight:700; color:#201B14;">Flux &middot; Morning briefing</div>
              <div style="font-family:{mono}; font-size:10.5px; color:#8A857C;">{date_display} &middot; {time_display} &middot; {project_display}</div>
            </div>
          </div>
          <div style="font-family:{serif}; font-size:24px; font-weight:500; line-height:1.2; color:#201B14; margin-bottom:6px;">
            {headline}
          </div>
          <div style="font-size:13px; color:#8A857C; margin-bottom:18px;">{summary_line}</div>

          <div style="border:1px solid #E0D6C3; border-radius:11px; overflow:hidden; margin-bottom:16px;">{finding_rows}
          </div>

          <div style="text-align:center;">
            <a href="{briefing_url}"
               style="display: inline-block; background: #C4703A; color: #FFF9EF; font-size: 14px;
                      font-weight: 600; padding: 12px 26px; border-radius: 9px; text-decoration: none;">
              Open the full briefing
            </a>
          </div>
        </div>
        <div style="padding: 14px 32px; border-top: 1px solid #EEE6D6; font-family: {mono};
                    font-size: 10.5px; color: #A89F8D; text-align: center;">
          Sent by Flux for {project_name} &middot;
          <a href="{briefing_url}" style="color: #8A857C; text-decoration: underline;">delivery settings</a> &middot;
          <a href="{briefing_url}" style="color: #8A857C; text-decoration: underline;">unsubscribe</a>
        </div>
      </div>
    </div>
    """

    text_lines = [headline, "", summary_line, ""]
    for f in findings:
        sev = (f.get("severity") or "").upper()
        title = f.get("title", "")
        body = f.get("body", "")
        text_lines.append(f"[{sev}] {title}" + (f" — {body}" if body else ""))
    text_lines += ["", f"Open the full briefing: {briefing_url}"]
    text_body = "\n".join(text_lines)

    await send_email(to_email, subject, html_body, text_body)

    # TODO(ledger-phase4): wire this into scheduled delivery once a
    # daily-digest scheduler exists. This codebase has no cron/scheduler
    # service today — briefing delivery is currently a Cowork-scheduled
    # recipe concept (see docs/plans/ledger-revamp-completion-plan.md
    # "Playbooks" and app/models/automation.py AutomationInstallation), not a
    # server-side job. The intended caller is a future daily job that, per
    # subscribed user+project, builds a findings list the same shape as the
    # local `briefing_findings` variable in
    # app/api/google_oauth_routes.py:home() (~line 631 onward) and calls
    # send_morning_briefing_email(...) here.
