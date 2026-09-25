"""
Pure (no-DB) tests for MCP PAT / headless support pieces.

These exercise the non-database parts that can be imported in isolation:
- OOB redirect_uri detection (used to decide whether to show a code page instead of client redirect)
- Token hint derivation logic (mirrors what create_pat does)
- Model column presence after migration 051
"""

import hashlib
import secrets

from app.auth.mcp_oauth_server import _is_oob_redirect_uri
from app.models.mcp_session import MCPSession


def _sha256(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _pat_hint(plaintext: str) -> str:
    return plaintext[:12] + "…" + plaintext[-4:] if len(plaintext) > 16 else plaintext[:8]


def test_oob_redirect_detection():
    assert _is_oob_redirect_uri("oob") is True
    assert _is_oob_redirect_uri("urn:ietf:wg:oauth:2.0:oob") is True
    assert _is_oob_redirect_uri("https://example.com/oauth/oob") is True
    assert _is_oob_redirect_uri("http://127.0.0.1:12345/cb/oob") is True  # contains oob
    assert _is_oob_redirect_uri("https://example.com/callback") is False
    assert _is_oob_redirect_uri(None) is False
    assert _is_oob_redirect_uri("") is False
    assert _is_oob_redirect_uri("https://claude.ai/cb") is False


def test_pat_token_format_and_hint():
    # Mirrors the generation in mcp_session_manager.create_pat
    plaintext = "fxt_pat_" + secrets.token_urlsafe(32)
    assert plaintext.startswith("fxt_pat_")
    assert len(plaintext) > 20
    hint = _pat_hint(plaintext)
    assert "…" in hint or len(hint) <= 8
    # Hash is what gets stored (manager.sha256 or identical impl)
    h = _sha256(plaintext)
    assert len(h) == 64  # hex sha256


def test_mcp_session_model_has_pat_columns():
    cols = {c.name for c in MCPSession.__table__.columns}
    assert "kind" in cols
    assert "name" in cols
    assert "token_hint" in cols
    # Existing columns still present
    assert "access_token_hash" in cols
    assert "is_revoked" in cols


def test_pat_hint_stability_examples():
    # 12 prefix + … + 4 suffix for a  fxt_pat_ + 32+ char token
    assert _pat_hint("fxt_pat_ABCDEF1234567890XYZ1234567890AB") == "fxt_pat_ABCD…90AB"
    assert _pat_hint("short") == "short"
