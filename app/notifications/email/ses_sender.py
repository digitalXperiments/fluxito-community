"""
Amazon SES sender — bring-your-own credentials.

Uses the sync ``boto3`` SES client wrapped in ``asyncio.to_thread``. We
stay off ``aioboto3`` to avoid the aiobotocore dependency tree (~15 MB)
for a worker that sends a handful of messages per day per project. The
SES API call is network-bound but fast; a single thread hop is fine.

We call ``send_raw_email`` rather than ``send_email`` so we can support
attachments uniformly with the SMTP path (``send_email`` cannot attach
files). The raw MIME payload is built with the stdlib ``email`` module
so both senders share the same MIME construction behaviour.

Config shape (the dict passed to ``SESSender(...)``):

    {
      "region":            "us-east-1",            # required — encrypted blob
      "access_key_id":     "AKIA…",                # required — encrypted blob
      "secret_access_key": "…",                    # required — encrypted blob
      "configuration_set": "my-config-set",        # optional — encrypted blob
      "from_email":        "reports@example.com",  # required — sender row column (verified in SES)
      "from_name":         "Analytics Reports",    # optional — sender row column
      "reply_to":          "team@example.com",     # optional
    }

The factory (``app.notifications.email.factory``) is responsible for
merging the encrypted blob (credentials) with the sender row's
``from_address`` / ``from_name`` columns before calling the constructor.

Note:
  The sender identity (``from_email`` or its domain) must be verified in
  the user's SES account and the account must be out of sandbox mode to
  send to arbitrary recipients. We cannot verify that client-side, so
  ``verify()`` only confirms that the credentials can reach SES; the
  first real send is where a misconfigured identity surfaces.
"""

from __future__ import annotations

import asyncio
import logging
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


class SESSender(EmailSender):
    """Amazon SES sender.

    boto3's SES client is synchronous; all network I/O here is hopped to
    a worker thread via ``asyncio.to_thread`` so the caller's event loop
    stays responsive.
    """

    def __init__(self, config: dict[str, Any]):
        missing = [
            k for k in ("region", "access_key_id", "secret_access_key", "from_email") if not config.get(k)
        ]
        if missing:
            raise EmailSendError(
                "SES config is missing required fields",
                detail=f"missing: {', '.join(missing)}",
            )

        self.region: str = str(config["region"]).strip()
        self.access_key_id: str = str(config["access_key_id"]).strip()
        self.secret_access_key: str = str(config["secret_access_key"])
        self.from_email: str = str(config["from_email"]).strip()
        self.from_name: str | None = (config.get("from_name") or "").strip() or None
        self.reply_to: str | None = (config.get("reply_to") or "").strip() or None
        self.configuration_set: str | None = (config.get("configuration_set") or "").strip() or None

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    async def send(self, message: EmailMessage) -> SendResult:
        mime = self._build_mime(message)
        envelope_recipients = message.primary_recipients()
        raw_bytes = mime.as_bytes()

        try:
            response = await asyncio.to_thread(self._send_raw, raw_bytes, envelope_recipients)
        except EmailSendError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("SES send failed region=%s: %s", self.region, exc)
            raise EmailSendError(
                "SES send failed",
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc

        return SendResult(
            success=True,
            message_id=(response or {}).get("MessageId"),
        )

    async def verify(self) -> SendResult:
        """Check that the credentials can reach SES (GetSendQuota).

        This does NOT verify that ``from_email`` is a verified identity —
        that's a property of the sender's SES account that only shows up
        when the first real send is attempted. We return a generic
        success as long as the API call itself succeeded.
        """
        try:
            await asyncio.to_thread(self._get_send_quota)
        except EmailSendError:
            raise
        except Exception as exc:
            logger.warning("SES verify failed region=%s: %s", self.region, exc)
            raise EmailSendError(
                "SES credential check failed",
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc
        return SendResult(success=True)

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    def _client(self):
        """Build a fresh boto3 SES client. Called from a worker thread."""
        import boto3  # lazy — do not import at module load, keeps cold start fast

        return boto3.client(
            "ses",
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )

    def _build_mime(self, message: EmailMessage) -> PyEmailMessage:
        """Same MIME construction as SMTP path — shared-style, for parity."""
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
        # Bcc omitted from headers, kept in envelope recipients.

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

        return py_msg

    def _send_raw(self, raw: bytes, recipients: list[str]) -> dict:
        """boto3 ``send_raw_email`` invocation. Runs in a worker thread.

        Translates common botocore exceptions into ``EmailSendError``
        with human-readable detail so the settings UI can surface
        actionable messages like "email address not verified".
        """
        try:
            from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
        except ImportError as exc:  # pragma: no cover
            raise EmailSendError(
                "boto3 is not installed",
                detail="install boto3 or switch this sender to SMTP",
            ) from exc

        kwargs: dict[str, Any] = {
            "Source": self.from_email,
            "Destinations": recipients,
            "RawMessage": {"Data": raw},
        }
        if self.configuration_set:
            kwargs["ConfigurationSetName"] = self.configuration_set

        try:
            return self._client().send_raw_email(**kwargs)
        except NoCredentialsError as exc:
            raise EmailSendError("SES credentials are invalid", detail=str(exc)) from exc
        except EndpointConnectionError as exc:
            raise EmailSendError("Could not reach SES", detail=f"region={self.region}") from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            # A handful of common codes get friendlier error messages.
            friendly = {
                "MessageRejected": "SES rejected the message (identity may not be verified)",
                "MailFromDomainNotVerifiedException": "SES sender domain is not verified",
                "AccessDenied": "SES credentials lack permission to send",
                "InvalidClientTokenId": "SES access key is invalid",
                "SignatureDoesNotMatch": "SES secret key does not match access key",
                "ConfigurationSetDoesNotExistException": "SES configuration set not found",
                "Throttling": "SES throttled the request — try again shortly",
            }
            raise EmailSendError(
                friendly.get(code, f"SES error: {code}"),
                detail=msg[:300],
            ) from exc

    def _get_send_quota(self) -> dict:
        """Cheap credential ping — hits SES GetSendQuota.

        Returns the quota dict on success; raises ``EmailSendError`` on
        any botocore failure with a friendly message.
        """
        try:
            from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
        except ImportError as exc:  # pragma: no cover
            raise EmailSendError(
                "boto3 is not installed",
                detail="install boto3 or switch this sender to SMTP",
            ) from exc

        try:
            return self._client().get_send_quota()
        except NoCredentialsError as exc:
            raise EmailSendError("SES credentials are invalid", detail=str(exc)) from exc
        except EndpointConnectionError as exc:
            raise EmailSendError("Could not reach SES", detail=f"region={self.region}") from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            msg = exc.response.get("Error", {}).get("Message", str(exc))
            raise EmailSendError(f"SES error: {code}", detail=msg[:300]) from exc
