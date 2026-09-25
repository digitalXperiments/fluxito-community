"""
SMTP sender — bring-your-own credentials.

Uses ``aiosmtplib``, a pure-Python async SMTP client. No native deps, no
retries, no persistent connections: every ``send`` opens a new session,
transmits, and closes. That's fine for our volume (scheduled reports are
low-frequency and test-send is one-off) and keeps the surface area tiny.

Config shape (the dict passed to ``SMTPSender(...)``):

    {
      "host":       "smtp.gmail.com",   # required — from encrypted blob
      "port":       587,                # required — from encrypted blob (465, 587, 25)
      "username":   "me@example.com",   # optional — from encrypted blob
      "password":   "…",                # optional — from encrypted blob
      "tls_mode":   "starttls",         # from encrypted blob
                                        #   'none' | 'starttls' | 'ssl' (port 465)
      "from_email": "reports@example.com",  # required — from sender row column
      "from_name":  "Analytics Reports",    # optional — from sender row column
      "reply_to":   "team@example.com",     # optional
      "timeout_sec": 20                     # optional, default 20
    }

The factory (``app.notifications.email.factory``) is responsible for
merging the encrypted blob (credentials) with the sender row's
``from_address`` / ``from_name`` columns before calling the constructor.
"""

from __future__ import annotations

import logging
import ssl
from email.message import EmailMessage as PyEmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from app.notifications.email.base import (
    EmailMessage,
    EmailSender,
    EmailSendError,
    SendResult,
)

logger = logging.getLogger(__name__)


class SMTPSender(EmailSender):
    """Async SMTP sender configured from a decrypted config dict.

    Do not instantiate with an encrypted blob — callers should decrypt
    via ``ProjectEmailSender.get_config()`` first. That keeps sender
    implementations completely unaware of the persistence layer.
    """

    # Canonical tls_mode values — ``ssl`` matches the stored convention
    # described in ``app.models.notification_channel``. ``tls`` is accepted as
    # a silent alias for forward compatibility.
    _VALID_TLS_MODES = {"none", "starttls", "ssl"}
    _TLS_MODE_ALIASES = {"tls": "ssl"}

    def __init__(self, config: dict[str, Any]):
        missing = [k for k in ("host", "port", "from_email") if not config.get(k)]
        if missing:
            raise EmailSendError(
                "SMTP config is missing required fields",
                detail=f"missing: {', '.join(missing)}",
            )

        tls_mode = (config.get("tls_mode") or "starttls").lower()
        tls_mode = self._TLS_MODE_ALIASES.get(tls_mode, tls_mode)
        if tls_mode not in self._VALID_TLS_MODES:
            raise EmailSendError(
                "SMTP config has an unsupported tls_mode",
                detail=f"got {tls_mode!r}, expected one of {sorted(self._VALID_TLS_MODES)}",
            )

        self.host: str = str(config["host"]).strip()
        self.port: int = int(config["port"])
        self.username: str | None = config.get("username") or None
        self.password: str | None = config.get("password") or None
        self.tls_mode: str = tls_mode
        self.from_email: str = str(config["from_email"]).strip()
        self.from_name: str | None = (config.get("from_name") or "").strip() or None
        self.reply_to: str | None = (config.get("reply_to") or "").strip() or None
        self.timeout_sec: int = int(config.get("timeout_sec") or 20)

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    async def send(self, message: EmailMessage) -> SendResult:
        py_msg, envelope_recipients = self._build_mime(message)
        try:
            await self._smtp_send(py_msg, envelope_recipients)
        except EmailSendError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("SMTP send failed host=%s port=%s: %s", self.host, self.port, exc)
            raise EmailSendError(
                "SMTP send failed",
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc

        return SendResult(success=True, message_id=py_msg.get("Message-ID"))

    async def verify(self) -> SendResult:
        """Credential-check only — no DATA phase, no message sent."""
        try:
            await self._smtp_verify()
        except EmailSendError:
            raise
        except Exception as exc:
            logger.warning("SMTP verify failed host=%s port=%s: %s", self.host, self.port, exc)
            raise EmailSendError(
                "SMTP connection failed",
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc
        return SendResult(success=True)

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    def _build_mime(self, message: EmailMessage) -> tuple[PyEmailMessage, list[str]]:
        """Translate our domain ``EmailMessage`` into an stdlib MIME message."""
        if not message.to:
            raise EmailSendError("Email has no recipients")
        if not message.subject:
            raise EmailSendError("Email is missing a subject")

        py_msg = PyEmailMessage()

        from_email = message.from_email or self.from_email
        from_name = message.from_name or self.from_name
        py_msg["From"] = formataddr((from_name, from_email)) if from_name else from_email

        py_msg["To"] = ", ".join(message.to)
        if message.cc:
            py_msg["Cc"] = ", ".join(message.cc)
        # Bcc intentionally not written to headers — only to the envelope.

        reply_to = message.reply_to or self.reply_to
        if reply_to:
            py_msg["Reply-To"] = reply_to

        py_msg["Subject"] = message.subject
        py_msg["Message-ID"] = make_msgid()

        py_msg.set_content(message.text_body or "")
        if message.html_body:
            py_msg.add_alternative(message.html_body, subtype="html")

        for att in message.attachments:
            maintype, _, subtype = (att.content_type or "application/octet-stream").partition("/")
            py_msg.add_attachment(
                att.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=att.filename,
            )

        return py_msg, message.primary_recipients()

    async def _smtp_send(self, py_msg: PyEmailMessage, recipients: list[str]) -> None:
        """Open an SMTP session, deliver, close. New connection every call."""
        import aiosmtplib

        tls_kwargs, start_tls = self._tls_kwargs()

        try:
            async with aiosmtplib.SMTP(
                hostname=self.host,
                port=self.port,
                timeout=self.timeout_sec,
                start_tls=start_tls,
                **tls_kwargs,
            ) as smtp:
                if self.username and self.password:
                    await smtp.login(self.username, self.password)
                await smtp.send_message(py_msg, recipients=recipients)
        except aiosmtplib.errors.SMTPAuthenticationError as exc:
            raise EmailSendError(
                "SMTP authentication failed",
                detail=f"{exc.code} {exc.message}" if hasattr(exc, "message") else str(exc),
            ) from exc
        except aiosmtplib.errors.SMTPConnectError as exc:
            raise EmailSendError(
                "Could not connect to SMTP server",
                detail=f"{self.host}:{self.port} — {exc}",
            ) from exc
        except aiosmtplib.errors.SMTPException as exc:
            raise EmailSendError(
                "SMTP server rejected the message",
                detail=str(exc)[:300],
            ) from exc

    async def _smtp_verify(self) -> None:
        """Connect + (optionally) AUTH + QUIT, no message body."""
        import aiosmtplib

        tls_kwargs, start_tls = self._tls_kwargs()

        try:
            async with aiosmtplib.SMTP(
                hostname=self.host,
                port=self.port,
                timeout=self.timeout_sec,
                start_tls=start_tls,
                **tls_kwargs,
            ) as smtp:
                if self.username and self.password:
                    await smtp.login(self.username, self.password)
        except aiosmtplib.errors.SMTPAuthenticationError as exc:
            raise EmailSendError(
                "SMTP authentication failed",
                detail=f"{exc.code} {exc.message}" if hasattr(exc, "message") else str(exc),
            ) from exc
        except aiosmtplib.errors.SMTPConnectError as exc:
            raise EmailSendError(
                "Could not connect to SMTP server",
                detail=f"{self.host}:{self.port} — {exc}",
            ) from exc

    def _tls_kwargs(self) -> tuple[dict, bool]:
        """
        Map our ``tls_mode`` to ``aiosmtplib`` kwargs.

        Returns ``(kwargs, start_tls)`` where:
          * kwargs  — passed via ``**`` into aiosmtplib.SMTP(...)
          * start_tls — True → upgrade a plaintext connection via STARTTLS

        aiosmtplib distinguishes ``use_tls`` (implicit TLS from the first
        byte — typically port 465) from ``start_tls`` (plaintext, then
        upgrade via STARTTLS — typically port 587).
        """
        if self.tls_mode == "ssl":
            ctx = ssl.create_default_context()
            return {"use_tls": True, "tls_context": ctx}, False
        if self.tls_mode == "starttls":
            ctx = ssl.create_default_context()
            return {"tls_context": ctx}, True
        # "none"
        return {}, False
