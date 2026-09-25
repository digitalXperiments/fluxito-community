"""
Email base types — message / attachment / sender protocol.

Concrete senders (``SMTPSender``, ``SESSender``) implement ``EmailSender``.
Call sites (scheduled-report worker, test-send endpoint) depend on this
module only — they should never import a concrete sender directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


class EmailSendError(Exception):
    """
    Raised when an email send fails.

    Carries a short, user-facing message plus an optional detail string so
    the settings UI can show something actionable ("SMTP auth failed",
    "SES rejected sender: not verified in region us-east-1"), not just
    "something broke".
    """

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}: {self.detail}"
        return self.message


@dataclass(frozen=True)
class EmailAttachment:
    """An in-memory attachment.

    We only ever handle small PDFs (a few hundred KB at most for a
    scheduled dashboard report), so keeping bytes in memory is fine. No
    streaming support — keeps the API tiny and avoids lifecycle bugs.

    Fields:
      filename      — display filename in the recipient's client
      content       — raw bytes of the attachment
      content_type  — MIME type (default: application/pdf)
    """

    filename: str
    content: bytes
    content_type: str = "application/pdf"


@dataclass(frozen=True)
class EmailMessage:
    """A ready-to-send email.

    ``to`` is a sequence so a single scheduled report can fan out to the
    whole recipient list in one SMTP conversation. ``text_body`` is
    required (for clients that don't render HTML) and ``html_body`` is
    optional but recommended.

    ``reply_to`` lets a project route replies to a shared inbox rather
    than the noreply address the SMTP/SES sender often uses.
    """

    to: Sequence[str]
    subject: str
    text_body: str
    html_body: str | None = None
    from_email: str | None = None  # override; defaults to sender's configured from
    from_name: str | None = None
    reply_to: str | None = None
    cc: Sequence[str] = field(default_factory=tuple)
    bcc: Sequence[str] = field(default_factory=tuple)
    attachments: Sequence[EmailAttachment] = field(default_factory=tuple)

    def primary_recipients(self) -> list[str]:
        """All envelope recipients (to + cc + bcc) — used by SMTP / SES."""
        return [*self.to, *self.cc, *self.bcc]


@dataclass(frozen=True)
class SendResult:
    """Result of a single ``EmailSender.send`` call."""

    success: bool
    message_id: str | None = None
    error: str | None = None


class EmailSender(Protocol):
    """Common contract every concrete sender implements.

    ``send`` is async so SMTP (``aiosmtplib``) can stay non-blocking and
    SES can hop to a thread via ``asyncio.to_thread`` without the caller
    needing to care about the distinction.

    Implementations should:
      * Raise ``EmailSendError`` for any failure — never bubble up
        provider-specific exceptions. Callers catch one type and show a
        consistent error UI.
      * Return ``SendResult(success=True, message_id=...)`` on success,
        filling ``message_id`` when the provider gives one.
      * Not retry internally — the scheduler decides whether a retry is
        warranted based on the failure kind and the schedule's history.
    """

    async def send(self, message: EmailMessage) -> SendResult: ...

    async def verify(self) -> SendResult:
        """
        Cheap credential-check that does NOT deliver mail.

        SMTP: connect + STARTTLS + AUTH + QUIT.
        SES:  call GetSendQuota or GetAccount.

        Used by the "Test connection" button in project settings and by
        the scheduler before auto-disabling a failing schedule (so we can
        distinguish "your credentials are bad" from "one recipient
        bounced").
        """
        ...
