"""
Auth & Security Tests

Covers:
  1. Password hashing (bcrypt round-trip, wrong password, corrupted hash)
  2. HMAC token generation & validation (verify tokens, reset tokens, expiry, tampering)
  3. CSRF double-submit cookie (token generation, signature verification, middleware logic)
"""

import time
from unittest.mock import patch

# ---------------------------------------------------------------------------
# 1. Password hashing (bcrypt)
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    """Tests for hash_password() and verify_password()."""

    def test_hash_and_verify_round_trip(self):
        from app.auth.email_auth import hash_password, verify_password

        hashed = hash_password("my-secure-password")
        assert hashed != "my-secure-password"
        assert hashed.startswith("$2b$")  # bcrypt prefix
        assert verify_password("my-secure-password", hashed) is True

    def test_wrong_password_fails(self):
        from app.auth.email_auth import hash_password, verify_password

        hashed = hash_password("correct-password")
        assert verify_password("wrong-password", hashed) is False

    def test_different_hashes_for_same_password(self):
        """bcrypt uses random salt, so two hashes of the same password differ."""
        from app.auth.email_auth import hash_password

        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2

    def test_corrupted_hash_returns_false(self):
        from app.auth.email_auth import verify_password

        assert verify_password("password", "not-a-valid-hash") is False

    def test_empty_password(self):
        from app.auth.email_auth import hash_password, verify_password

        hashed = hash_password("")
        assert verify_password("", hashed) is True
        assert verify_password("non-empty", hashed) is False


# ---------------------------------------------------------------------------
# 2. HMAC token generation & validation
# ---------------------------------------------------------------------------


class TestHMACTokens:
    """Tests for _make_token / _verify_token and the verify/reset wrappers."""

    def test_verify_token_round_trip(self):
        from app.auth.email_auth import generate_verify_token, validate_verify_token

        user_id = "550e8400-e29b-41d4-a716-446655440000"
        token = generate_verify_token(user_id)
        assert validate_verify_token(token) == user_id

    def test_reset_token_round_trip(self):
        from app.auth.email_auth import generate_reset_token, validate_reset_token

        user_id = "550e8400-e29b-41d4-a716-446655440001"
        token = generate_reset_token(user_id)
        assert validate_reset_token(token) == user_id

    def test_verify_token_expired(self):
        """Token older than 24h should be rejected."""
        from app.auth.email_auth import VERIFY_TTL, _make_token, validate_verify_token

        user_id = "test-user-id"
        # Fake a token created 25 hours ago
        with patch("app.auth.email_auth.time") as mock_time:
            mock_time.time.return_value = time.time() - VERIFY_TTL - 3600
            token = _make_token(user_id, "verify_email", VERIFY_TTL)

        assert validate_verify_token(token) is None

    def test_reset_token_expired(self):
        """Token older than 1h should be rejected."""
        from app.auth.email_auth import RESET_TTL, _make_token, validate_reset_token

        user_id = "test-user-id"
        with patch("app.auth.email_auth.time") as mock_time:
            mock_time.time.return_value = time.time() - RESET_TTL - 600
            token = _make_token(user_id, "reset_password", RESET_TTL)

        assert validate_reset_token(token) is None

    def test_tampered_signature_rejected(self):
        from app.auth.email_auth import generate_verify_token, validate_verify_token

        token = generate_verify_token("some-user-id")
        # Tamper with the signature (last segment)
        parts = token.rsplit(".", 1)
        tampered = parts[0] + ".0000000000000000000000000000000000000000000000000000000000000000"
        assert validate_verify_token(tampered) is None

    def test_tampered_user_id_rejected(self):
        from app.auth.email_auth import generate_verify_token, validate_verify_token

        token = generate_verify_token("original-user")
        # Replace user_id portion
        parts = token.split(".", 2)
        parts[0] = "attacker-user"
        tampered = ".".join(parts)
        assert validate_verify_token(tampered) is None

    def test_garbage_token_rejected(self):
        from app.auth.email_auth import validate_reset_token, validate_verify_token

        assert validate_verify_token("not.a.valid.token") is None
        assert validate_verify_token("") is None
        assert validate_verify_token("no-dots-at-all") is None
        assert validate_reset_token("garbage") is None

    def test_wrong_purpose_rejected(self):
        """A verify token should not validate as a reset token and vice versa."""
        from app.auth.email_auth import (
            generate_reset_token,
            generate_verify_token,
            validate_reset_token,
            validate_verify_token,
        )

        verify_tok = generate_verify_token("user-1")
        reset_tok = generate_reset_token("user-1")

        # Cross-purpose validation should fail
        assert validate_reset_token(verify_tok) is None
        assert validate_verify_token(reset_tok) is None

    def test_token_format(self):
        """Token should be {user_id}.{timestamp}.{hex_signature}."""
        from app.auth.email_auth import generate_verify_token

        token = generate_verify_token("my-user-id")
        parts = token.split(".")
        assert len(parts) == 3
        assert parts[0] == "my-user-id"
        assert parts[1].isdigit()
        # Signature should be a hex string
        int(parts[2], 16)  # Should not raise


# ---------------------------------------------------------------------------
# 3. CSRF protection
# ---------------------------------------------------------------------------


class TestCSRF:
    """Tests for CSRF token generation, verification, and middleware logic."""

    def test_generate_token_format(self):
        from app.auth.csrf import _generate_csrf_token

        token = _generate_csrf_token()
        assert "." in token
        raw, sig = token.rsplit(".", 1)
        assert len(raw) == 64  # token_hex(32)
        assert len(sig) == 16  # truncated HMAC

    def test_verify_valid_token(self):
        from app.auth.csrf import _generate_csrf_token, _verify_csrf_token

        token = _generate_csrf_token()
        assert _verify_csrf_token(token) is True

    def test_verify_tampered_token(self):
        from app.auth.csrf import _generate_csrf_token, _verify_csrf_token

        token = _generate_csrf_token()
        raw, _sig = token.rsplit(".", 1)
        tampered = raw + ".0000000000000000"
        assert _verify_csrf_token(tampered) is False

    def test_verify_garbage(self):
        from app.auth.csrf import _verify_csrf_token

        assert _verify_csrf_token("") is False
        assert _verify_csrf_token("no-dot") is False
        assert _verify_csrf_token("a.b.c") is False

    def test_tokens_are_unique(self):
        from app.auth.csrf import _generate_csrf_token

        tokens = {_generate_csrf_token() for _ in range(50)}
        assert len(tokens) == 50

    def test_exempt_paths(self):
        """Verify that exempt path prefixes are correctly defined."""
        from app.auth.csrf import _EXEMPT_PREFIXES

        assert "/mcp" in _EXEMPT_PREFIXES
        assert "/oauth/" in _EXEMPT_PREFIXES
        assert "/auth/google/" in _EXEMPT_PREFIXES
        assert "/static/" in _EXEMPT_PREFIXES
        assert "/.well-known/" in _EXEMPT_PREFIXES

    def test_safe_methods(self):
        from app.auth.csrf import _SAFE_METHODS

        assert "GET" in _SAFE_METHODS
        assert "HEAD" in _SAFE_METHODS
        assert "OPTIONS" in _SAFE_METHODS
        assert "POST" not in _SAFE_METHODS
        assert "DELETE" not in _SAFE_METHODS

    def test_all_csrf_cookie_values_parses_duplicates(self):
        """Multiple csrf_token cookies (different scoping) must all be seen."""
        from types import SimpleNamespace

        from app.auth.csrf import _all_csrf_cookie_values

        req = SimpleNamespace(headers={"cookie": "uid=abc; csrf_token=AAA; foo=1; csrf_token=BBB"})
        assert _all_csrf_cookie_values(req) == ["AAA", "BBB"]

    def test_validate_double_submit_accepts_any_matching_cookie(self):
        """Header matching ANY sent cookie value (with valid sig) passes — the
        fix for the duplicate-cookie 'token mismatch' bug."""
        from app.auth.csrf import _generate_csrf_token, _validate_double_submit

        stale = "deadbeef.0000000000000000"  # legacy/duplicate cookie, bad sig
        good = _generate_csrf_token()
        # Browser sends both cookies; JS submitted the good one as the header.
        assert _validate_double_submit([stale, good], good) is None
        # Order-independent.
        assert _validate_double_submit([good, stale], good) is None

    def test_validate_double_submit_accepts_coalesced_identical_headers(self):
        """Duplicate X-CSRF-Token headers can arrive comma-coalesced."""
        from app.auth.csrf import _generate_csrf_token, _validate_double_submit

        good = _generate_csrf_token()
        assert _validate_double_submit([good], f"{good}, {good}") is None

    def test_validate_double_submit_rejects_coalesced_mixed_headers(self):
        from app.auth.csrf import _generate_csrf_token, _validate_double_submit

        good = _generate_csrf_token()
        other = _generate_csrf_token()
        msg = _validate_double_submit([good], f"{good}, {other}")
        assert msg is not None and "mismatch" in msg

    def test_validate_double_submit_rejects_unknown_header(self):
        from app.auth.csrf import _generate_csrf_token, _validate_double_submit

        good = _generate_csrf_token()
        other = _generate_csrf_token()
        # Header matches no cookie the browser sent → mismatch.
        msg = _validate_double_submit([good], other)
        assert msg is not None and "mismatch" in msg

    def test_validate_double_submit_missing(self):
        from app.auth.csrf import _generate_csrf_token, _validate_double_submit

        assert "missing" in _validate_double_submit([], _generate_csrf_token())
        assert "missing" in _validate_double_submit([_generate_csrf_token()], None)

    def test_validate_double_submit_rejects_unsigned_match(self):
        """A matching cookie+header that we never minted (bad signature) fails."""
        from app.auth.csrf import _validate_double_submit

        forged = "deadbeef.0000000000000000"
        msg = _validate_double_submit([forged], forged)
        assert msg is not None and "Invalid" in msg
