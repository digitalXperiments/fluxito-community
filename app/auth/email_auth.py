"""
Email/password authentication service.

Handles:
  - Password hashing and verification (bcrypt via passlib)
  - Registration (create user with hashed password)
  - Email verification tokens (HMAC-based, 24h TTL)
  - Password reset tokens (HMAC-based, 1h TTL)
  - Sending verification and reset emails
"""

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime

import bcrypt
from sqlalchemy import select

import app.app_state as app_state
from app.config import settings
from app.email_service import send_email
from app.models.user import User

logger = logging.getLogger(__name__)

# Token TTL constants (seconds)
_VERIFY_EMAIL_TTL = 24 * 3600  # 24 hours
_RESET_PASSWORD_TTL = 3600  # 1 hour

# Public aliases for tests
VERIFY_TTL = _VERIFY_EMAIL_TTL
RESET_TTL = _RESET_PASSWORD_TTL


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:
        return False


def generate_temp_password() -> str:
    """Generate a strong, URL-safe temporary password for invited users."""
    return secrets.token_urlsafe(12)


# ---------------------------------------------------------------------------
# Token generation (HMAC-based, no DB storage needed)
# ---------------------------------------------------------------------------


def _make_token(user_id: str, purpose: str, ttl_seconds: int) -> str:
    """
    Generate a signed token: {user_id}.{timestamp}.{signature}

    Signature is HMAC-SHA256(secret, user_id + purpose + timestamp).
    """
    ts = str(int(time.time()))
    payload = f"{user_id}:{purpose}:{ts}"
    sig = hmac.new(settings.APP_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{ts}.{sig}"


def _verify_token(token: str, purpose: str, ttl_seconds: int) -> str | None:
    """
    Verify a token and return the user_id, or None if invalid/expired.
    """
    try:
        user_id, ts_str, sig = token.split(".", 2)
        ts = int(ts_str)

        # Check expiry
        if time.time() - ts > ttl_seconds:
            return None

        # Verify signature
        payload = f"{user_id}:{purpose}:{ts_str}"
        expected = hmac.new(settings.APP_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()

        if hmac.compare_digest(sig, expected):
            return user_id
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


def generate_verify_token(user_id: str) -> str:
    """Generate a 24-hour email verification token."""
    return _make_token(user_id, "verify_email", _VERIFY_EMAIL_TTL)


def validate_verify_token(token: str) -> str | None:
    """Validate a verification token. Returns user_id or None."""
    return _verify_token(token, "verify_email", _VERIFY_EMAIL_TTL)


async def send_verification_email(email: str, user_id: str, base_url: str):
    """Send the verification email with a link."""
    token = generate_verify_token(user_id)
    verify_url = f"{base_url}/auth/verify-email?token={token}"

    html_body = f"""
    <div style="font-family: Inter, -apple-system, system-ui, sans-serif; max-width: 560px; margin: 0 auto; padding: 32px 0;">
      <div style="text-align: center; margin-bottom: 32px;">
        <span style="font-family: 'JetBrains Mono', monospace; font-size: 20px; font-weight: 700; color: #1c1917;">
          [ Fluxito ]
        </span>
      </div>

      <div style="background: #fffefa; border: 1px solid #e4e2dc; border-radius: 12px; padding: 32px;">
        <h2 style="font-size: 20px; font-weight: 700; margin: 0 0 8px; color: #1c1917;">
          Verify your email
        </h2>
        <p style="font-size: 15px; color: #57534e; margin: 0 0 24px; line-height: 1.6;">
          Click the button below to verify your email address and activate your account.
        </p>

        <div style="text-align: center; margin: 24px 0;">
          <a href="{verify_url}"
             style="display: inline-block; background: #b47800; color: #fff; font-size: 15px;
                    font-weight: 600; padding: 12px 32px; border-radius: 8px;
                    text-decoration: none;">
            Verify Email
          </a>
        </div>

        <p style="font-size: 13px; color: #a8a29e; margin: 24px 0 0; line-height: 1.5;">
          This link expires in 24 hours. If you didn't create an account on Fluxito,
          you can safely ignore this email.
        </p>
      </div>

      <p style="font-size: 12px; color: #a8a29e; text-align: center; margin-top: 24px;">
        Fluxito — Marketing analytics for any AI
      </p>
    </div>
    """

    text_body = (
        f"Verify your email for Fluxito\n\n"
        f"Click this link to verify your account: {verify_url}\n\n"
        f"This link expires in 24 hours."
    )

    await send_email(email, "Verify your email — Fluxito", html_body, text_body)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def generate_reset_token(user_id: str) -> str:
    """Generate a 1-hour password reset token."""
    return _make_token(user_id, "reset_password", _RESET_PASSWORD_TTL)


def validate_reset_token(token: str) -> str | None:
    """Validate a reset token. Returns user_id or None."""
    return _verify_token(token, "reset_password", _RESET_PASSWORD_TTL)


async def send_reset_email(email: str, user_id: str, base_url: str):
    """Send a password reset email."""
    token = generate_reset_token(user_id)
    reset_url = f"{base_url}/auth/reset-password?token={token}"

    html_body = f"""
    <div style="font-family: Inter, -apple-system, system-ui, sans-serif; max-width: 560px; margin: 0 auto; padding: 32px 0;">
      <div style="text-align: center; margin-bottom: 32px;">
        <span style="font-family: 'JetBrains Mono', monospace; font-size: 20px; font-weight: 700; color: #1c1917;">
          [ Fluxito ]
        </span>
      </div>

      <div style="background: #fffefa; border: 1px solid #e4e2dc; border-radius: 12px; padding: 32px;">
        <h2 style="font-size: 20px; font-weight: 700; margin: 0 0 8px; color: #1c1917;">
          Reset your password
        </h2>
        <p style="font-size: 15px; color: #57534e; margin: 0 0 24px; line-height: 1.6;">
          We received a request to reset the password for your account. Click the
          button below to choose a new password.
        </p>

        <div style="text-align: center; margin: 24px 0;">
          <a href="{reset_url}"
             style="display: inline-block; background: #b47800; color: #fff; font-size: 15px;
                    font-weight: 600; padding: 12px 32px; border-radius: 8px;
                    text-decoration: none;">
            Reset Password
          </a>
        </div>

        <p style="font-size: 13px; color: #a8a29e; margin: 24px 0 0; line-height: 1.5;">
          This link expires in 1 hour. If you didn't request a password reset,
          you can safely ignore this email.
        </p>
      </div>

      <p style="font-size: 12px; color: #a8a29e; text-align: center; margin-top: 24px;">
        Fluxito — Marketing analytics for any AI
      </p>
    </div>
    """

    text_body = (
        f"Reset your Fluxito password\n\n"
        f"Click this link to reset your password: {reset_url}\n\n"
        f"This link expires in 1 hour."
    )

    await send_email(email, "Reset your password — Fluxito", html_body, text_body)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def register_user(
    email: str,
    password: str,
    display_name: str | None = None,
) -> tuple[User | None, str | None]:
    """
    Register a new user with email/password.

    Returns (user, error). On success error is None; on failure user is None.
    If an account already owns the email, returns an error (existing rows are
    never claimed or modified — that would be an account-takeover vector, since
    a password-less row may be a Google-only user, not just an invited stub).
    """
    async with app_state.db_session_factory() as db:
        result = await db.execute(select(User).where(User.email == email))
        existing = result.scalar_one_or_none()

        if existing is not None:
            if existing.auth_provider == "google":
                return (
                    None,
                    "This email is already registered with Google sign-in. Please use Google to sign in, or link a password from your profile.",
                )
            return None, "An account with this email already exists. Try signing in instead."

        user = User(
            email=email,
            display_name=display_name,
            password_hash=hash_password(password),
            email_verified=False,
            auth_provider="email",
        )
        db.add(user)
        await db.flush()
        await db.commit()

    return user, None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def authenticate_user(email: str, password: str) -> tuple[User | None, str | None]:
    """
    Authenticate a user by email/password.

    Returns (user, error). Checks password, active status, and email verification.
    """
    async with app_state.db_session_factory() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if not user:
            return None, "No account found with this email."

        if not user.password_hash:
            # Google-only user — no password set
            return (
                None,
                "This account uses Google sign-in. Please sign in with Google, or set a password from your profile.",
            )

        if not verify_password(password, user.password_hash):
            return None, "Incorrect password."

        if user.deactivated_by_admin:
            return None, "This account has been deactivated by an administrator."
        if not user.is_active:
            # The user paused their own account; signing in again reactivates it.
            user.is_active = True
            await db.commit()
            from app.auth import account_status

            account_status.forget(str(user.id))

        if not user.email_verified:
            return None, "UNVERIFIED"  # special code to trigger resend UI

        return user, None


# ---------------------------------------------------------------------------
# Verify email
# ---------------------------------------------------------------------------


async def verify_user_email(token: str) -> tuple[bool, str | None]:
    """
    Process an email verification token.

    Returns (success, error_message).
    """
    user_id = validate_verify_token(token)
    if not user_id:
        return False, "Invalid or expired verification link. Please request a new one."

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            return False, "User not found."

        if user.email_verified:
            return True, None  # already verified — that's fine

        user.email_verified = True
        user.email_verified_at = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()

    return True, None


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------


async def reset_user_password(token: str, new_password: str) -> tuple[bool, str | None]:
    """
    Reset a user's password using a valid reset token.

    Returns (success, error_message).
    """
    user_id = validate_reset_token(token)
    if not user_id:
        return False, "Invalid or expired reset link. Please request a new one."

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            return False, "User not found."

        user.password_hash = hash_password(new_password)
        # If they're resetting password, also verify email (they clicked the link)
        if not user.email_verified:
            user.email_verified = True
            user.email_verified_at = datetime.now(UTC).replace(tzinfo=None)
        # Update auth_provider if was Google-only
        if user.auth_provider == "google":
            user.auth_provider = "both"
        await db.commit()

    return True, None
