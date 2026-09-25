"""
MCP Session Manager

Validates MCP Bearer tokens on every tool call.
Provides:
  - sha256() helper
  - UserContext dataclass
  - require_valid_mcp_token() FastAPI dependency
  - inject_user_context() ASGI middleware for /mcp prefix

Performance optimizations:
  - Token validation cached fully in Redis (no DB hit on cache hit)
  - UserContext cached in Redis with 2-min TTL
  - last_used_at updates batched via Redis (flushed periodically)
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import Request
from fastapi.exceptions import HTTPException
from sqlalchemy import select, update

import app.app_state as app_state
from app.models.bq_connection import BQConnection
from app.models.connection import OAuthConnection
from app.models.credential_connection import (
    AdjustConnection,
    AdobeConnection,
    AmplitudeConnection,
    AppsFlyerConnection,
    BranchConnection,
    BrazeConnection,
    MarketoConnection,
    MixpanelConnection,
    MoengageConnection,
    PostHogConnection,
    RedshiftConnection,
    SnowflakeConnection,
)
from app.models.mcp_session import MCPSession
from app.models.project import Project, ProjectMember
from app.models.token import GA4Property, GoogleAdsAccount, GTMContainer, SearchConsoleSite
from app.models.user import User

logger = logging.getLogger(__name__)

# Semaphore to bound concurrent fire-and-forget DB writes
_update_semaphore = asyncio.Semaphore(5)

# TTL for cached user context in Redis (seconds)
_USER_CTX_CACHE_TTL = 120
_PROJECT_CTX_CACHE_TTL = 120


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# Google scope constants for platform detection
_GA4_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/analytics",
        "https://www.googleapis.com/auth/analytics.edit",
    }
)
_GTM_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/tagmanager.readonly",
        "https://www.googleapis.com/auth/tagmanager.edit.containers",
        "https://www.googleapis.com/auth/tagmanager.publish",
        "https://www.googleapis.com/auth/tagmanager.manage.accounts",
    }
)
_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
_GSC_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/webmasters.readonly",
        "https://www.googleapis.com/auth/webmasters",
    }
)
_DATA_MANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"


def derive_google_platform_flags(google_connections) -> tuple[bool, bool, bool, bool]:
    """Derive (has_ga4, has_gtm, has_ads, has_gsc) from Google OAuth connections."""
    all_scopes: set = set()
    for c in google_connections:
        for s in c.scopes or []:
            all_scopes.add(s)
    has_ga4 = bool(all_scopes & _GA4_SCOPES)
    has_gtm = bool(all_scopes & _GTM_SCOPES)
    has_ads = _ADS_SCOPE in all_scopes
    has_gsc = bool(all_scopes & _GSC_SCOPES)
    return has_ga4, has_gtm, has_ads, has_gsc


def _apply_provider_filter(connections, eff):
    """Drop connections whose provider the effective permissions don't allow.
    Accepts dicts or objects exposing a ``provider`` key/attr. full/None -> unchanged."""
    if eff is None or getattr(eff, "full", False):
        return connections

    def _prov(c):
        return c.get("provider") if isinstance(c, dict) else getattr(c, "provider", None)

    def _allowed(c) -> bool:
        provider = _prov(c)
        if provider == "google":
            # One OAuthConnection backs several Google products, including
            # Data Manager. Keep it when any permitted Google product needs it.
            return any(
                eff.allows_provider(product)
                for product in ("ga4", "gtm", "google_ads", "gsc", "data_manager")
            )
        return provider is not None and eff.allows_provider(provider)

    return [c for c in connections if _allowed(c)]


@dataclass
class ConnectionInfo:
    id: str
    provider: str
    google_email: str | None
    scopes: list[str]
    connection_status: str
    access_token_encrypted: str | None = None
    refresh_token_encrypted: str | None = None


@dataclass
class ProjectMembership:
    """Lightweight summary of a project a user belongs to."""

    project_id: str
    project_name: str
    project_slug: str
    role: str  # 'owner' | 'admin' | 'member'
    is_active: bool = True


@dataclass
class UserContext:
    """
    Identity + project memberships. Loaded once per MCP session.

    Does NOT contain connections — those live in ProjectContext, which is
    set when the user picks an active project via ``set_active_project``.
    The legacy ``has_*`` flags and ``connections`` list are still populated
    for backward compatibility during the migration period — they will
    reflect the connections of the *active project* once set.
    """

    user_id: str
    email: str
    display_name: str | None
    # Project memberships
    projects: list[ProjectMembership] = field(default_factory=list)
    # Fine-grained Google flags — derived from scopes on the OAuth connection
    has_ga4: bool = False  # analytics.readonly or analytics scope granted
    has_gtm: bool = False  # tagmanager scope granted
    has_ads: bool = False  # adwords scope granted
    has_gsc: bool = False  # webmasters(.readonly) scope granted
    has_data_manager: bool = False  # Data Manager audience scope granted
    # Non-Google platforms
    has_bq: bool = False
    has_meta: bool = False
    has_tiktok: bool = False
    has_snap: bool = False
    has_linkedin: bool = False
    has_pinterest: bool = False
    has_x: bool = False
    has_reddit: bool = False
    has_bing: bool = False
    has_apple: bool = False
    has_amplitude: bool = False
    has_branch: bool = False
    has_appsflyer: bool = False
    has_adjust: bool = False
    has_mixpanel: bool = False
    has_posthog: bool = False
    has_braze: bool = False
    has_moengage: bool = False
    has_adobe_analytics: bool = False
    has_adobe_launch: bool = False
    has_adobe_marketo: bool = False
    has_redshift: bool = False
    has_snowflake: bool = False
    connections: list[ConnectionInfo] = field(default_factory=list)
    ga4_properties: list[dict] = field(default_factory=list)
    gtm_containers: list[dict] = field(default_factory=list)
    ads_accounts: list[dict] = field(default_factory=list)
    search_console_sites: list[dict] = field(default_factory=list)

    @property
    def has_google(self) -> bool:
        """True if any Google OAuth connection is active (any scope)."""
        return bool(self.connections)

    def to_cache_dict(self) -> dict:
        """Serialize to a JSON-safe dict for Redis caching."""
        d = asdict(self)
        return d

    @classmethod
    def from_cache_dict(cls, data: dict) -> "UserContext":
        """Restore from a cached dict."""
        conns = [ConnectionInfo(**c) for c in data.pop("connections", [])]
        projects = [ProjectMembership(**p) for p in data.pop("projects", [])]
        return cls(connections=conns, projects=projects, **data)


@dataclass
class ProjectContext:
    """
    Active project context — loaded when a user calls ``set_active_project``.
    Contains the project's role and all connections/resources.
    Tools read this to scope all operations to a single project.
    """

    project_id: str
    project_name: str
    project_slug: str
    role: str  # caller's role in this project
    owner_id: str
    # Platform flags (derived from this project's connections)
    has_ga4: bool = False
    has_gtm: bool = False
    has_ads: bool = False
    has_gsc: bool = False
    has_data_manager: bool = False
    has_bq: bool = False
    has_meta: bool = False
    has_tiktok: bool = False
    has_snap: bool = False
    has_linkedin: bool = False
    has_pinterest: bool = False
    has_x: bool = False
    has_reddit: bool = False
    has_bing: bool = False
    has_apple: bool = False
    has_amplitude: bool = False
    has_branch: bool = False
    has_appsflyer: bool = False
    has_adjust: bool = False
    has_mixpanel: bool = False
    has_posthog: bool = False
    has_braze: bool = False
    has_moengage: bool = False
    has_adobe_analytics: bool = False
    has_adobe_launch: bool = False
    has_adobe_marketo: bool = False
    has_redshift: bool = False
    has_snowflake: bool = False
    # Connection details
    connections: list[ConnectionInfo] = field(default_factory=list)
    ga4_properties: list[dict] = field(default_factory=list)
    gtm_containers: list[dict] = field(default_factory=list)
    ads_accounts: list[dict] = field(default_factory=list)
    search_console_sites: list[dict] = field(default_factory=list)

    @property
    def has_google(self) -> bool:
        return any(c.provider == "google" for c in self.connections)

    def to_cache_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_cache_dict(cls, data: dict) -> "ProjectContext":
        conns = [ConnectionInfo(**c) for c in data.pop("connections", [])]
        return cls(connections=conns, **data)


async def _validate_token(token: str, request: Request) -> MCPSession:
    """
    Validate MCP bearer token.
    Optimized: Redis stores full session metadata so cache hits skip DB entirely.
    """
    token_hash = sha256(token)
    redis = app_state.redis_client
    cache_key = f"mcp_session:{token_hash}"

    # ── Redis cache check (full session data, not just user_id) ──
    cached = await redis.get(cache_key)
    if cached:
        data = json.loads(cached)
        # Quick expiry check on cached data
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at < datetime.utcnow():
            await redis.delete(cache_key)
            raise HTTPException(401, "Token expired")
        if data.get("is_revoked"):
            raise HTTPException(401, "Token revoked")

        # Return a lightweight object — no DB hit needed
        # We create a minimal MCPSession-like object for downstream code
        session = MCPSession()
        session.id = UUID(data["session_id"])
        session.user_id = UUID(data["user_id"])
        session.access_token_hash = token_hash
        session.access_token_expires_at = expires_at
        session.is_revoked = False
        session.client_id = data.get("client_id")
        session.scopes = data.get("scopes")

        # Batch last_used update via Redis (flushed periodically, not per-request)
        await redis.zadd("mcp:last_used", {str(session.id): time.time()})
        return session

    # ── DB fallback ──
    async with app_state.db_session_factory() as db:
        result = await db.execute(select(MCPSession).where(MCPSession.access_token_hash == token_hash))
        session = result.scalar_one_or_none()

        if not session:
            raise HTTPException(401, "Invalid token")
        if session.is_revoked:
            raise HTTPException(401, "Token revoked")
        if session.access_token_expires_at < datetime.utcnow():
            raise HTTPException(401, "Token expired")

        # Cache FULL session metadata in Redis
        ttl = int((session.access_token_expires_at - datetime.utcnow()).total_seconds())
        cache_data = json.dumps(
            {
                "session_id": str(session.id),
                "user_id": str(session.user_id),
                "client_id": session.client_id,
                "expires_at": session.access_token_expires_at.isoformat(),
                "is_revoked": session.is_revoked,
                "scopes": list(session.scopes) if session.scopes else None,
            }
        )
        await redis.setex(cache_key, max(ttl, 1), cache_data)

        # Batch last_used update
        await redis.zadd("mcp:last_used", {str(session.id): time.time()})

        return session


async def _resolve_client_name(client_id: str | None) -> str | None:
    """
    Look up a friendly client name from `mcp_clients` for a given client_id.
    Cached in Redis long-term since client names rarely change. Applies
    light normalization so "Claude" / "claude-ai" / "ChatGPT" show nicely.
    """
    if not client_id:
        return None

    redis = app_state.redis_client
    cache_key = f"mcp_client_name:{client_id}"

    try:
        cached = await redis.get(cache_key)
    except Exception:
        cached = None
    if cached:
        name = cached.decode() if isinstance(cached, bytes) else cached
        return name or None

    name: str | None = None
    try:
        from app.models.connection import MCPClient

        async with app_state.db_session_factory() as db:
            result = await db.execute(select(MCPClient).where(MCPClient.client_id == client_id))
            client = result.scalar_one_or_none()
            if client and client.client_name:
                name = client.client_name
    except Exception:
        name = None

    # Normalize common patterns so the UI reads nicely
    if name:
        low = name.lower()
        if "claude" in low:
            name = "Claude"
        elif "chatgpt" in low or "openai" in low:
            name = "ChatGPT"
        elif "cursor" in low:
            name = "Cursor"
        elif name.startswith("Dynamic Client") or name.startswith("Auto-registered"):
            # Fall back to client_id prefix for anonymous dynamic clients
            name = f"MCP client ({client_id[:8]})"

    # Fallback: derive from client_id itself
    if not name and client_id:
        cid_low = client_id.lower()
        if "claude" in cid_low:
            name = "Claude"
        elif "chatgpt" in cid_low or "openai" in cid_low:
            name = "ChatGPT"
        elif "cursor" in cid_low:
            name = "Cursor"
        else:
            name = f"MCP client ({client_id[:8]})"

    try:
        await redis.setex(cache_key, 3600, name or "")
    except Exception:
        pass
    return name


_LAST_USED_FLUSH_BATCH = 1000


async def flush_last_used_batch():
    """
    Flush batched last_used_at updates from Redis to DB.
    Call this periodically (e.g., every 30s from a background task).
    """
    redis = app_state.redis_client
    try:
        entries = await redis.zpopmin("mcp:last_used", count=_LAST_USED_FLUSH_BATCH)
        if not entries:
            return

        async with app_state.db_session_factory() as db:
            for session_id_bytes, timestamp in entries:
                session_id = (
                    session_id_bytes.decode()
                    if isinstance(session_id_bytes, bytes)
                    else str(session_id_bytes)
                )
                try:
                    await db.execute(
                        update(MCPSession)
                        .where(MCPSession.id == UUID(session_id))
                        .values(last_used_at=datetime.utcfromtimestamp(float(timestamp)))
                    )
                except Exception:
                    pass  # Skip invalid entries
            await db.commit()
    except Exception as e:
        logger.warning(f"Failed to flush last_used batch: {e}")


# ---------------------------------------------------------------------------
# PAT (Personal Access Token) helpers for headless / remote MCP clients
# These create long-lived bearer tokens that are stored as MCPSession rows
# (kind='pat') so they flow through the exact same _validate_token /
# require_valid_mcp_token / build_user_context paths as OAuth sessions.
# Plaintext token is returned only once from create_pat().
# ---------------------------------------------------------------------------


async def create_pat(
    user_id: str,
    name: str,
    expires_in_days: int | None,
) -> dict:
    """Create a PAT for the given user.

    - name: human label (required, trimmed, <=255)
    - expires_in_days: 30/60/90/365 or None for "never" (far-future sentinel)
    Returns: {id, token (plaintext, once only), token_hint, name, expires_at, created_at}
    The plaintext is never persisted or logged.
    """
    if not name or not str(name).strip():
        raise ValueError("name is required")
    name = str(name).strip()[:255]

    plaintext = "fxt_pat_" + secrets.token_urlsafe(32)
    token_hash = sha256(plaintext)

    now = datetime.utcnow()
    if expires_in_days is None or int(expires_in_days) <= 0:
        # Far-future sentinel per PAT design (matches "no expiry" option)
        expires_at = datetime(9999, 12, 31)
    else:
        expires_at = now + timedelta(days=int(expires_in_days))

    # Short display hint, e.g. fxt_pat_AbCd...wXyZ
    hint = plaintext[:12] + "…" + plaintext[-4:] if len(plaintext) > 16 else plaintext[:8]

    db_session = app_state.db_session_factory()
    async with db_session as db:
        row = MCPSession(
            user_id=UUID(str(user_id)),
            access_token_hash=token_hash,
            refresh_token_hash=None,
            client_id="pat",
            scopes=["read", "write"],
            access_token_expires_at=expires_at,
            refresh_token_expires_at=None,
            kind="pat",
            name=name,
            token_hint=hint,
            is_revoked=False,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

    return {
        "id": str(row.id),
        "token": plaintext,  # returned exactly once
        "token_hint": hint,
        "name": name,
        "expires_at": None if expires_at.year >= 9999 else expires_at.isoformat(),
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
    }


async def list_pats(user_id: str, active_only: bool = True) -> list[dict]:
    """List PATs for the user.
    By default only active (non-revoked) ones are returned, since revoked tokens
    are useless for the user and hidden from the UI.
    Use active_only=False if you need to see history/revoked for some reason.
    Never returns plaintext or hashes.
    """
    db_session = app_state.db_session_factory()
    async with db_session as db:
        where = [
            MCPSession.user_id == UUID(str(user_id)),
            MCPSession.kind == "pat",
        ]
        if active_only:
            where.append(MCPSession.is_revoked == False)
        result = await db.execute(select(MCPSession).where(*where).order_by(MCPSession.created_at.desc()))
        rows = result.scalars().all()

    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "id": str(r.id),
                "name": r.name,
                "token_hint": r.token_hint,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "access_token_expires_at": r.access_token_expires_at.isoformat()
                if r.access_token_expires_at
                else None,
                "is_revoked": bool(r.is_revoked),
            }
        )
    return out


async def revoke_pat(user_id: str, pat_id: str) -> bool:
    """Revoke a PAT owned by the user. Immediate effect (DB flag + Redis purge).
    Returns True if a row was found+revoked.
    """
    uid = UUID(str(user_id))
    pid = UUID(str(pat_id))

    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(
            select(MCPSession).where(
                MCPSession.id == pid,
                MCPSession.user_id == uid,
                MCPSession.kind == "pat",
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            return False

        access_hash = row.access_token_hash
        await db.execute(update(MCPSession).where(MCPSession.id == pid).values(is_revoked=True))
        await db.commit()

    # Purge cache so subsequent validation fails immediately (matches oauth revoke behavior)
    try:
        redis = app_state.redis_client
        if access_hash:
            await redis.delete(f"mcp_session:{access_hash}")
    except Exception:
        pass

    return True


def _oauth_session_is_live(row: MCPSession, now: datetime) -> bool:
    """An OAuth session is usable while its access or refresh token is unexpired."""
    if row.access_token_expires_at and row.access_token_expires_at > now:
        return True
    return bool(row.refresh_token_expires_at and row.refresh_token_expires_at > now)


async def list_oauth_clients(user_id: str) -> list[dict]:
    """List AI clients the user has authorized over OAuth, one row per client.

    Groups the user's live (non-revoked, unexpired) ``kind='oauth'`` sessions
    by ``client_id`` and resolves the registered client name. Never returns
    token material.
    """
    from app.models.connection import MCPClient

    now = datetime.utcnow()
    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(
            select(MCPSession).where(
                MCPSession.user_id == UUID(str(user_id)),
                MCPSession.kind == "oauth",
                MCPSession.is_revoked == False,
            )
        )
        rows = [r for r in result.scalars().all() if _oauth_session_is_live(r, now)]
        client_ids = sorted({r.client_id for r in rows if r.client_id})
        names: dict[str, str] = {}
        if client_ids:
            name_rows = await db.execute(
                select(MCPClient.client_id, MCPClient.client_name).where(MCPClient.client_id.in_(client_ids))
            )
            names = {str(cid): str(cname) for cid, cname in name_rows.all() if cid}

    grouped: dict[str, dict] = {}
    for r in rows:
        cid = r.client_id or "unknown"
        entry = grouped.setdefault(
            cid,
            {
                "client_id": cid,
                "client_name": names.get(cid) or cid,
                "sessions": 0,
                "authorized_at": None,
                "last_used_at": None,
            },
        )
        entry["sessions"] += 1
        if r.created_at and (entry["authorized_at"] is None or r.created_at < entry["authorized_at"]):
            entry["authorized_at"] = r.created_at
        if r.last_used_at and (entry["last_used_at"] is None or r.last_used_at > entry["last_used_at"]):
            entry["last_used_at"] = r.last_used_at

    out = []
    for entry in grouped.values():
        out.append(
            {
                **entry,
                "authorized_at": entry["authorized_at"].isoformat() if entry["authorized_at"] else None,
                "last_used_at": entry["last_used_at"].isoformat() if entry["last_used_at"] else None,
            }
        )
    out.sort(key=lambda e: e["last_used_at"] or e["authorized_at"] or "", reverse=True)
    return out


async def revoke_oauth_client(user_id: str, client_id: str) -> int:
    """Revoke every OAuth session the user granted to ``client_id``.

    Immediate effect (DB flag + Redis purge). Returns the number of sessions
    revoked; 0 means the user had no live sessions for that client.
    """
    uid = UUID(str(user_id))
    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(
            select(MCPSession).where(
                MCPSession.user_id == uid,
                MCPSession.kind == "oauth",
                MCPSession.client_id == client_id,
                MCPSession.is_revoked == False,
            )
        )
        rows = list(result.scalars().all())
        if not rows:
            return 0
        hashes = [r.access_token_hash for r in rows if r.access_token_hash]
        await db.execute(
            update(MCPSession).where(MCPSession.id.in_([r.id for r in rows])).values(is_revoked=True)
        )
        await db.commit()

    try:
        redis = app_state.redis_client
        for h in hashes:
            await redis.delete(f"mcp_session:{h}")
    except Exception:
        pass
    return len(rows)


async def _load_connections_and_resources(
    db,
    owner_filter_column,
    owner_id,
) -> tuple[
    list,
    list,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    list,
    list,
    list,
    list,
]:
    """
    Load connections and Google resources for a user or project.
    Shared by build_user_context and build_project_context to eliminate duplication.

    Returns: (
        all_connections_orm, google_connections,
        has_bq, has_amplitude, has_branch, has_appsflyer, has_adjust,
        has_mixpanel, has_posthog,
        has_braze, has_moengage,
        has_adobe_analytics, has_adobe_launch,
        has_adobe_marketo, has_redshift, has_snowflake, has_meta, has_tiktok, has_snap,
        has_linkedin, has_pinterest, has_x, has_reddit, has_bing, has_apple,
        has_ga4, has_gtm, has_ads, has_gsc,
        ga4_props, gtm_cons, ads_accs, gsc_sites
    )
    """
    # Load ALL active connections
    result = await db.execute(
        select(OAuthConnection).where(
            owner_filter_column == owner_id,
            OAuthConnection.is_active == True,
        )
    )
    all_connections_orm = result.scalars().all()

    # Derive platform flags from connections
    providers = {c.provider or "google" for c in all_connections_orm}
    has_meta = "meta" in providers
    has_tiktok = "tiktok" in providers
    has_snap = "snap" in providers
    has_linkedin = "linkedin" in providers
    has_pinterest = "pinterest" in providers
    has_x = "x" in providers
    has_reddit = "reddit" in providers
    has_bing = "bing" in providers
    has_apple = "apple" in providers

    # Check credential-based connections.
    #
    # These live in their OWN tables, so each MUST be scoped by THAT table's
    # own owner column — NOT by the OAuthConnection column passed in. Filtering
    # a `select(BQConnection)` by `OAuthConnection.project_id` produces a
    # cartesian product that returns every tenant's rows whenever the caller has
    # any oauth_connection, leaking warehouse/credential connection *presence*
    # across projects and users. BQ's Fluxito-project column is
    # ``fluxito_project_id``; the rest use ``project_id``.
    _scope_by_project = getattr(owner_filter_column, "key", None) == "project_id"

    def _cred_scope(model):
        if _scope_by_project:
            col = model.fluxito_project_id if model is BQConnection else model.project_id
        else:
            col = model.user_id
        return col == owner_id

    result = await db.execute(
        select(BQConnection).where(_cred_scope(BQConnection), BQConnection.is_active == True).limit(1)
    )
    has_bq = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(AmplitudeConnection)
        .where(_cred_scope(AmplitudeConnection), AmplitudeConnection.is_active == True)
        .limit(1)
    )
    has_amplitude = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(BranchConnection)
        .where(_cred_scope(BranchConnection), BranchConnection.is_active == True)
        .limit(1)
    )
    has_branch = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(AppsFlyerConnection)
        .where(_cred_scope(AppsFlyerConnection), AppsFlyerConnection.is_active == True)
        .limit(1)
    )
    has_appsflyer = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(AdjustConnection)
        .where(_cred_scope(AdjustConnection), AdjustConnection.is_active == True)
        .limit(1)
    )
    has_adjust = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(AdobeConnection).where(_cred_scope(AdobeConnection), AdobeConnection.is_active == True)
    )
    adobe_conns = result.scalars().all()
    has_adobe_analytics = any(c.has_analytics for c in adobe_conns)
    has_adobe_launch = any(c.has_launch for c in adobe_conns)

    result = await db.execute(
        select(MarketoConnection)
        .where(_cred_scope(MarketoConnection), MarketoConnection.is_active == True)
        .limit(1)
    )
    has_adobe_marketo = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(RedshiftConnection)
        .where(_cred_scope(RedshiftConnection), RedshiftConnection.is_active == True)
        .limit(1)
    )
    has_redshift = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(SnowflakeConnection)
        .where(_cred_scope(SnowflakeConnection), SnowflakeConnection.is_active == True)
        .limit(1)
    )
    has_snowflake = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(MixpanelConnection)
        .where(_cred_scope(MixpanelConnection), MixpanelConnection.is_active == True)
        .limit(1)
    )
    has_mixpanel = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(PostHogConnection)
        .where(_cred_scope(PostHogConnection), PostHogConnection.is_active == True)
        .limit(1)
    )
    has_posthog = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(BrazeConnection)
        .where(_cred_scope(BrazeConnection), BrazeConnection.is_active == True)
        .limit(1)
    )
    has_braze = result.scalar_one_or_none() is not None

    result = await db.execute(
        select(MoengageConnection)
        .where(_cred_scope(MoengageConnection), MoengageConnection.is_active == True)
        .limit(1)
    )
    has_moengage = result.scalar_one_or_none() is not None

    # Filter to Google connections for scope-checking and resource loading
    google_connections = [c for c in all_connections_orm if (c.provider or "google") == "google"]

    ga4_props = []
    gtm_cons = []
    ads_accs = []
    gsc_sites = []
    has_ga4 = False
    has_gtm = False
    has_ads = False
    has_gsc = False

    if google_connections:
        google_connection_ids = [c.id for c in google_connections]

        ga4_result = await db.execute(
            select(GA4Property).where(
                GA4Property.connection_id.in_(google_connection_ids),
                GA4Property.is_active == True,
            )
        )
        ga4_props = [
            {
                "property_id": p.property_id,
                "property_name": p.property_name,
                "account_id": p.account_id,
                "account_name": p.account_name,
                "connection_id": str(p.connection_id),
            }
            for p in ga4_result.scalars().all()
        ]

        gtm_result = await db.execute(
            select(GTMContainer).where(
                GTMContainer.connection_id.in_(google_connection_ids),
                GTMContainer.is_active == True,
            )
        )
        gtm_cons = [
            {
                "account_id": c.account_id,
                "container_id": c.container_id,
                "container_name": c.container_name,
                "public_id": c.public_id,
                "connection_id": str(c.connection_id),
            }
            for c in gtm_result.scalars().all()
        ]

        ads_result = await db.execute(
            select(GoogleAdsAccount).where(
                GoogleAdsAccount.connection_id.in_(google_connection_ids),
                GoogleAdsAccount.is_active == True,
            )
        )
        ads_accs = [
            {
                "customer_id": a.customer_id,
                "account_name": a.account_name,
                "currency_code": a.currency_code,
                "timezone": a.timezone,
                "connection_id": str(a.connection_id),
            }
            for a in ads_result.scalars().all()
        ]

        gsc_result = await db.execute(
            select(SearchConsoleSite).where(
                SearchConsoleSite.connection_id.in_(google_connection_ids),
                SearchConsoleSite.is_active == True,
            )
        )
        gsc_sites = [
            {
                "site_url": s.site_url,
                "permission_level": s.permission_level,
                "is_domain_property": s.is_domain_property,
                "connection_id": str(s.connection_id),
            }
            for s in gsc_result.scalars().all()
        ]

        has_ga4, has_gtm, has_ads, has_gsc = derive_google_platform_flags(google_connections)

    return (
        all_connections_orm,
        google_connections,
        has_bq,
        has_amplitude,
        has_branch,
        has_appsflyer,
        has_adjust,
        has_mixpanel,
        has_posthog,
        has_braze,
        has_moengage,
        has_adobe_analytics,
        has_adobe_launch,
        has_adobe_marketo,
        has_redshift,
        has_snowflake,
        has_meta,
        has_tiktok,
        has_snap,
        has_linkedin,
        has_pinterest,
        has_x,
        has_reddit,
        has_bing,
        has_apple,
        has_ga4,
        has_gtm,
        has_ads,
        has_gsc,
        ga4_props,
        gtm_cons,
        ads_accs,
        gsc_sites,
    )


async def count_google_resources_for_connections(db, connection_ids: list) -> dict[str, int]:
    """Count GA4/GTM/Ads/GSC resources across a set of Google OAuth connections.

    Used by the Connect UI to display "N GA4 properties, M GTM containers"
    counts without requiring callers to re-duplicate the four SELECTs the
    session manager already owns. Runs the four queries concurrently.
    """
    from sqlalchemy import func as _sqlfunc

    from app.models.token import GA4Property, GoogleAdsAccount, GTMContainer, SearchConsoleSite

    if not connection_ids:
        return {"ga4": 0, "gtm": 0, "ads": 0, "gsc": 0}

    async def _count(model):
        res = await db.execute(
            select(_sqlfunc.count()).select_from(model).where(model.connection_id.in_(connection_ids))
        )
        return res.scalar_one() or 0

    ga4_n, gtm_n, ads_n, gsc_n = await asyncio.gather(
        _count(GA4Property),
        _count(GTMContainer),
        _count(GoogleAdsAccount),
        _count(SearchConsoleSite),
    )
    return {"ga4": ga4_n, "gtm": gtm_n, "ads": ads_n, "gsc": gsc_n}


async def build_user_context(user_id: str, request: Request = None) -> UserContext:
    """
    Load user + their connections/properties into a UserContext.
    Cached in Redis for 2 minutes to avoid redundant DB queries.
    """
    redis = app_state.redis_client
    cache_key = f"user_ctx:{user_id}"

    # ── Check Redis cache first ──
    cached = await redis.get(cache_key)
    if cached:
        try:
            return UserContext.from_cache_dict(json.loads(cached))
        except Exception:
            await redis.delete(cache_key)  # Clear corrupt cache

    # ── DB queries (consolidated into single session) ──
    async with app_state.db_session_factory() as db:
        # Load user
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(401, "User not found")

        # Load project memberships
        result = await db.execute(
            select(ProjectMember, Project)
            .join(Project, ProjectMember.project_id == Project.id)
            .where(
                ProjectMember.user_id == user.id,
                ProjectMember.is_active == True,
                Project.is_active == True,
            )
        )
        project_memberships = [
            ProjectMembership(
                project_id=str(proj.id),
                project_name=proj.name,
                project_slug=proj.slug,
                role=pm.role,
                is_active=pm.is_active,
            )
            for pm, proj in result.all()
        ]

        # Load connections and resources via shared helper
        (
            all_connections_orm,
            _google_connections,
            has_bq,
            has_amplitude,
            has_branch,
            has_appsflyer,
            has_adjust,
            has_mixpanel,
            has_posthog,
            has_braze,
            has_moengage,
            has_adobe_analytics,
            has_adobe_launch,
            has_adobe_marketo,
            has_redshift,
            has_snowflake,
            has_meta,
            has_tiktok,
            has_snap,
            has_linkedin,
            has_pinterest,
            has_x,
            has_reddit,
            has_bing,
            has_apple,
            has_ga4,
            has_gtm,
            has_ads,
            has_gsc,
            ga4_props,
            gtm_cons,
            ads_accs,
            gsc_sites,
        ) = await _load_connections_and_resources(db, OAuthConnection.user_id, user.id)

        connections = [
            ConnectionInfo(
                id=str(c.id),
                provider=c.provider or "google",
                google_email=c.google_email,
                scopes=c.scopes or [],
                connection_status=c.connection_status,
                access_token_encrypted=c.access_token_encrypted,
                refresh_token_encrypted=c.refresh_token_encrypted,
            )
            for c in all_connections_orm
        ]

        ctx = UserContext(
            user_id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            projects=project_memberships,
            has_ga4=has_ga4,
            has_gtm=has_gtm,
            has_ads=has_ads,
            has_gsc=has_gsc,
            has_data_manager=any(
                _DATA_MANAGER_SCOPE in (c.scopes or [])
                for c in all_connections_orm
                if (c.provider or "google") == "google"
            ),
            has_bq=has_bq,
            has_meta=has_meta,
            has_tiktok=has_tiktok,
            has_snap=has_snap,
            has_linkedin=has_linkedin,
            has_pinterest=has_pinterest,
            has_x=has_x,
            has_reddit=has_reddit,
            has_bing=has_bing,
            has_apple=has_apple,
            has_amplitude=has_amplitude,
            has_branch=has_branch,
            has_appsflyer=has_appsflyer,
            has_adjust=has_adjust,
            has_mixpanel=has_mixpanel,
            has_posthog=has_posthog,
            has_braze=has_braze,
            has_moengage=has_moengage,
            has_adobe_analytics=has_adobe_analytics,
            has_adobe_launch=has_adobe_launch,
            has_adobe_marketo=has_adobe_marketo,
            has_redshift=has_redshift,
            has_snowflake=has_snowflake,
            connections=connections,
            ga4_properties=ga4_props,
            gtm_containers=gtm_cons,
            ads_accounts=ads_accs,
            search_console_sites=gsc_sites,
        )

        # Cache in Redis
        await redis.setex(cache_key, _USER_CTX_CACHE_TTL, json.dumps(ctx.to_cache_dict()))
        return ctx


async def build_project_context(project_id: str, user_id: str) -> ProjectContext:
    """
    Load a project's connections, resources, and the caller's role into
    a ProjectContext. Cached in Redis for 2 minutes.

    Raises HTTPException(403) if the user is not a member of the project.
    Raises HTTPException(404) if the project doesn't exist.
    """
    redis = app_state.redis_client
    cache_key = f"project_ctx:{project_id}:{user_id}"

    # ── Check Redis cache first ──
    cached = await redis.get(cache_key)
    if cached:
        try:
            return ProjectContext.from_cache_dict(json.loads(cached))
        except Exception:
            await redis.delete(cache_key)

    async with app_state.db_session_factory() as db:
        # Load project
        result = await db.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(404, "Project not found")

        # Verify membership and get role
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == user_id,
                ProjectMember.is_active == True,
            )
        )
        membership = result.scalar_one_or_none()
        if not membership:
            raise HTTPException(403, "You are not a member of this project")

        # Load connections and resources via shared helper
        (
            all_connections_orm,
            _google_connections,
            has_bq,
            has_amplitude,
            has_branch,
            has_appsflyer,
            has_adjust,
            has_mixpanel,
            has_posthog,
            has_braze,
            has_moengage,
            has_adobe_analytics,
            has_adobe_launch,
            has_adobe_marketo,
            has_redshift,
            has_snowflake,
            has_meta,
            has_tiktok,
            has_snap,
            has_linkedin,
            has_pinterest,
            has_x,
            has_reddit,
            has_bing,
            has_apple,
            has_ga4,
            has_gtm,
            has_ads,
            has_gsc,
            ga4_props,
            gtm_cons,
            ads_accs,
            gsc_sites,
        ) = await _load_connections_and_resources(db, OAuthConnection.project_id, project.id)

        # ── RBAC provider filtering ──
        # Resolve effective permissions for the caller (fails gracefully).
        eff = None
        try:
            from app.auth.permissions import resolve_effective_permissions

            eff = await resolve_effective_permissions(str(user_id), str(project_id))
        except Exception as _rbac_err:
            logger.warning("RBAC permission resolution failed, skipping provider filter: %s", _rbac_err)

        if eff is not None and not eff.full:
            # Filter OAuthConnection-based rows (google/meta/tiktok/snap/linkedin/
            # pinterest/x/reddit/bing/apple). provider=None means google.
            filtered_orm = _apply_provider_filter(all_connections_orm, eff)

            # Re-derive flags that come from OAuthConnection.provider
            filtered_providers = {c.provider or "google" for c in filtered_orm}
            has_meta = "meta" in filtered_providers
            has_tiktok = "tiktok" in filtered_providers
            has_snap = "snap" in filtered_providers
            has_linkedin = "linkedin" in filtered_providers
            has_pinterest = "pinterest" in filtered_providers
            has_x = "x" in filtered_providers
            has_reddit = "reddit" in filtered_providers
            has_bing = "bing" in filtered_providers
            has_apple = "apple" in filtered_providers

            # Gate credential-based flags by provider grant
            has_bq = has_bq and eff.allows_provider("bigquery")
            has_amplitude = has_amplitude and eff.allows_provider("amplitude")
            has_branch = has_branch and eff.allows_provider("branch")
            has_appsflyer = has_appsflyer and eff.allows_provider("appsflyer")
            has_adjust = has_adjust and eff.allows_provider("adjust")
            has_mixpanel = has_mixpanel and eff.allows_provider("mixpanel")
            has_posthog = has_posthog and eff.allows_provider("posthog")
            has_braze = has_braze and eff.allows_provider("braze")
            has_moengage = has_moengage and eff.allows_provider("moengage")
            has_adobe_analytics = has_adobe_analytics and eff.allows_provider("adobe_analytics")
            has_adobe_launch = has_adobe_launch and eff.allows_provider("adobe_launch")
            has_adobe_marketo = has_adobe_marketo and eff.allows_provider("adobe_marketo")
            has_redshift = has_redshift and eff.allows_provider("redshift")
            has_snowflake = has_snowflake and eff.allows_provider("snowflake")

            # Gate Google-scope-derived flags by provider grant
            has_ga4 = has_ga4 and eff.allows_provider("ga4")
            has_gtm = has_gtm and eff.allows_provider("gtm")
            has_ads = has_ads and eff.allows_provider("google_ads")
            has_gsc = has_gsc and eff.allows_provider("gsc")

            all_connections_orm = filtered_orm

        connections = [
            ConnectionInfo(
                id=str(c.id),
                provider=c.provider or "google",
                google_email=c.google_email,
                scopes=c.scopes or [],
                connection_status=c.connection_status,
                access_token_encrypted=c.access_token_encrypted,
                refresh_token_encrypted=c.refresh_token_encrypted,
            )
            for c in all_connections_orm
        ]

        ctx = ProjectContext(
            project_id=str(project.id),
            project_name=project.name,
            project_slug=project.slug,
            role=membership.role,
            owner_id=str(project.owner_id),
            has_ga4=has_ga4,
            has_gtm=has_gtm,
            has_ads=has_ads,
            has_gsc=has_gsc,
            has_data_manager=any(
                _DATA_MANAGER_SCOPE in (c.scopes or [])
                for c in all_connections_orm
                if (c.provider or "google") == "google"
            ),
            has_bq=has_bq,
            has_meta=has_meta,
            has_tiktok=has_tiktok,
            has_snap=has_snap,
            has_linkedin=has_linkedin,
            has_pinterest=has_pinterest,
            has_x=has_x,
            has_reddit=has_reddit,
            has_bing=has_bing,
            has_apple=has_apple,
            has_amplitude=has_amplitude,
            has_branch=has_branch,
            has_appsflyer=has_appsflyer,
            has_adjust=has_adjust,
            has_mixpanel=has_mixpanel,
            has_posthog=has_posthog,
            has_braze=has_braze,
            has_moengage=has_moengage,
            has_adobe_analytics=has_adobe_analytics,
            has_adobe_launch=has_adobe_launch,
            has_adobe_marketo=has_adobe_marketo,
            has_redshift=has_redshift,
            has_snowflake=has_snowflake,
            connections=connections,
            ga4_properties=ga4_props,
            gtm_containers=gtm_cons,
            ads_accounts=ads_accs,
            search_console_sites=gsc_sites,
        )

        await redis.setex(cache_key, _PROJECT_CTX_CACHE_TTL, json.dumps(ctx.to_cache_dict()))
        return ctx


def invalidate_user_context_cache(user_id: str):
    """
    Call this whenever a user's connections change (add/remove platform).
    Can be called fire-and-forget: asyncio.create_task(invalidate_user_context_cache_async(user_id))
    """
    return _invalidate_ctx_cache(user_id)


async def _invalidate_ctx_cache(user_id: str):
    """Async helper to clear cached UserContext."""
    try:
        await app_state.redis_client.delete(f"user_ctx:{user_id}")
    except Exception:
        pass


async def invalidate_project_context_cache(project_id: str):
    """
    Call when a project's connections change. Clears all cached
    ProjectContexts for this project (wildcard on user_id portion).
    """
    try:
        redis = app_state.redis_client
        # Scan for all keys matching project_ctx:{project_id}:*
        async for key in redis.scan_iter(f"project_ctx:{project_id}:*"):
            await redis.delete(key)
    except Exception:
        pass


async def require_valid_mcp_token(request: Request) -> UserContext:
    """
    FastAPI dependency — validates MCP Bearer token and returns UserContext.
    Use this in route handlers that need auth.

    Side effect: stashes the resolved MCP client_name on
    ``request.state.mcp_client_name`` so middleware/routes can propagate it
    into ``app_state.current_client_name_ctx`` for activity logging.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")

    token = auth_header[7:]
    session = await _validate_token(token, request)

    from app.auth import account_status

    if await account_status.blocked_for_mcp(str(session.user_id)):
        raise HTTPException(401, "Account deactivated")

    # Tokens from before scopes were recorded behave as read + write.
    try:
        request.state.mcp_scopes = list(session.scopes) if session.scopes else ["read", "write"]
    except Exception:
        pass

    # Resolve and stash client name for downstream activity logging
    try:
        client_name = await _resolve_client_name(getattr(session, "client_id", None))
        request.state.mcp_client_name = client_name
    except Exception:
        request.state.mcp_client_name = None

    return await build_user_context(str(session.user_id), request)


def no_active_project_response() -> dict:
    """Standard response when no project is active in the MCP session."""
    return {
        "error": True,
        "error_type": "no_active_project",
        "message": "No active project. Use set_active_project to select a project first.",
        "action_required": "Call list_my_projects to see your projects, then set_active_project('project name or id') to select one.",
    }


def require_project_ctx() -> Optional["ProjectContext"]:
    """
    Helper for tool handlers — returns the active ProjectContext or None.
    Tools should call this and return no_active_project_response() if None.
    """
    return app_state.current_project_ctx.get()


# ---------------------------------------------------------------------------
# Per-call active-project resolution
# ---------------------------------------------------------------------------
#
# MCP is stateless HTTP and the tool-call hook (see registry._install_tool_hook)
# runs every tool inside its own asyncio task via ``asyncio.wait_for``. A new
# task copies the current context at creation time, so a
# ``current_project_ctx.set()`` performed by ``set_active_project`` in ONE tool
# call never reaches a sibling call in the same request/batch. Relying on that
# ContextVar across calls is therefore unsound.
#
# The durable source of truth is Redis (written by ``set_active_project``) plus
# any explicit per-call ``project_id``. ``ensure_call_project_ctx`` resolves
# both, once per call, inside the hook's task — so EVERY tool sees a consistent
# active project regardless of ordering, batching, or which ContextVar it reads.

# Tools that must NOT have their project context auto-resolved (they manage it
# themselves or are project-agnostic).
_PROJECT_CTX_SKIP_TOOLS = frozenset({"set_active_project"})

# Project-context flags mirrored onto the UserContext shim so legacy read tools
# that fall back to ``user_ctx`` (analytics_read, marketing_read, ...) reflect
# the active project's connections rather than the user's own.
_PROJECT_FLAG_ATTRS = (
    "has_ga4",
    "has_gtm",
    "has_ads",
    "has_gsc",
    "has_data_manager",
    "has_bq",
    "has_meta",
    "has_tiktok",
    "has_snap",
    "has_linkedin",
    "has_pinterest",
    "has_x",
    "has_reddit",
    "has_bing",
    "has_apple",
    "has_amplitude",
    "has_branch",
    "has_appsflyer",
    "has_adjust",
    "has_mixpanel",
    "has_posthog",
    "has_braze",
    "has_moengage",
    "has_adobe_analytics",
    "has_adobe_launch",
    "has_adobe_marketo",
    "has_redshift",
    "has_snowflake",
    "connections",
    "ga4_properties",
    "gtm_containers",
    "ads_accounts",
    "search_console_sites",
)


def _is_project_member(user_ctx, project_id: str) -> bool:
    return hasattr(user_ctx, "projects") and any(p.project_id == project_id for p in user_ctx.projects)


def _match_membership_project_id(user_ctx, query: str) -> str | None:
    """Exact-match a project by id, slug, or name against the caller's memberships.

    Deliberately stricter than ``set_active_project``'s fuzzy match: this runs
    silently on every tool call, so we only honor unambiguous identifiers and
    never a partial-name guess (which could hijack scope off a stray argument).
    """
    if not query or not hasattr(user_ctx, "projects"):
        return None
    q = query.strip().lower()
    for p in user_ctx.projects:
        if q == p.project_id.lower() or q == p.project_slug.lower() or q == p.project_name.lower():
            return p.project_id
    return None


def _extract_explicit_project(arguments) -> str | None:
    """Pull an explicit Fluxito project identifier from tool arguments.

    Checks top-level ``project_id`` / ``project`` and the nested ``params`` dict
    (the unified dispatchers wrap real args inside ``params``). A value that
    doesn't match a membership (e.g. an Amplitude ``project_id``) is ignored by
    the caller, so this is safe to read broadly.
    """
    if not isinstance(arguments, dict):
        return None
    cand = arguments.get("project_id") or arguments.get("project")
    if not cand:
        params = arguments.get("params")
        if isinstance(params, dict):
            cand = params.get("project_id") or params.get("project")
    return str(cand) if cand else None


def _sync_project_flags_onto_user(user_ctx, pctx) -> None:
    """Mirror the active project's connection flags onto the UserContext shim."""
    for attr in _PROJECT_FLAG_ATTRS:
        if hasattr(pctx, attr):
            setattr(user_ctx, attr, getattr(pctx, attr))


async def ensure_call_project_ctx(tool_name: str, arguments) -> object | None:
    """Resolve and set ``current_project_ctx`` for a single tool call.

    Resolution order:
      1. Explicit ``project_id`` / ``project`` argument matching one of the
         caller's projects → per-call override (fully stateless).
      2. Otherwise, if a project is already active for this request → keep it.
      3. Otherwise, restore the session's last-selected project from Redis.

    Returns a ContextVar token to ``reset`` after the call, or ``None`` when
    nothing was set (caller must guard the reset on a non-None token).
    """
    if tool_name in _PROJECT_CTX_SKIP_TOOLS:
        return None
    user_ctx = app_state.current_user_ctx.get()
    if user_ctx is None:
        return None

    target_pid: str | None = None

    explicit = _extract_explicit_project(arguments)
    if explicit:
        target_pid = _match_membership_project_id(user_ctx, explicit)
        # A non-matching explicit value is not a Fluxito project — fall through
        # to session/Redis resolution rather than blocking it.

    if target_pid is None:
        current = app_state.current_project_ctx.get()
        if current is not None:
            return None  # already resolved for this request — keep it
        try:
            redis = app_state.redis_client
            if redis is not None:
                cached = await redis.get(f"mcp:active_project:{user_ctx.user_id}")
                if cached:
                    pid = cached.decode() if isinstance(cached, bytes) else str(cached)
                    if _is_project_member(user_ctx, pid):
                        target_pid = pid
        except Exception:
            return None

    if not target_pid:
        return None

    current = app_state.current_project_ctx.get()
    if current is not None and current.project_id == target_pid:
        return None  # no change needed

    try:
        pctx = await build_project_context(target_pid, user_ctx.user_id)
    except Exception:
        return None

    token = app_state.current_project_ctx.set(pctx)
    _sync_project_flags_onto_user(user_ctx, pctx)
    return token


def _no_connection(base_url: str, message: str, connect_path: str = "/connect") -> dict:
    """Factory for standard 'connection_missing' error responses."""
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": message,
        "connect_url": f"{base_url}{connect_path}",
        "action_required": f"Visit {base_url}{connect_path} to connect.",
    }


def no_google_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Google account connected.", "/connect")


def no_ga4_response(base_url: str) -> dict:
    return _no_connection(
        base_url,
        "GA4 access not granted. Your Google connection is missing analytics scopes.",
        "/connect",
    )


def no_gtm_response(base_url: str) -> dict:
    return _no_connection(
        base_url,
        "GTM access not granted. Your Google connection is missing Tag Manager scopes.",
        "/connect",
    )


def no_ads_response(base_url: str) -> dict:
    return _no_connection(
        base_url,
        "Google Ads access not granted. Your Google connection is missing the adwords scope.",
        "/connect",
    )


def no_gsc_response(base_url: str) -> dict:
    return _no_connection(
        base_url,
        "Search Console access not granted. Your Google connection is missing the webmasters scope.",
        "/connect/google",
    )


def no_bing_response(base_url: str) -> dict:
    return _no_connection(
        base_url,
        "No Bing Webmaster Tools connection found. Connect your Microsoft account to get Bing search data.",
        "/connect/bing",
    )


def no_amplitude_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Amplitude connection found.", "/connect/amplitude")


def no_branch_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Branch connection found.", "/connect/branch")


def no_appsflyer_response(base_url: str) -> dict:
    return _no_connection(base_url, "No AppsFlyer connection found.", "/connect/appsflyer")


def no_adjust_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Adjust connection found.", "/connect/adjust")


def no_mixpanel_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Mixpanel connection found.", "/connect/mixpanel")


def no_posthog_response(base_url: str) -> dict:
    return _no_connection(base_url, "No PostHog connection found.", "/connect/posthog")


def no_braze_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Braze connection found.", "/connect/braze")


def no_moengage_response(base_url: str) -> dict:
    return _no_connection(base_url, "No MoEngage connection found.", "/connect/moengage")


def no_adobe_analytics_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Adobe Analytics connection found.", "/connect/adobe")


def no_adobe_launch_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Adobe Launch connection found.", "/connect/adobe")


def no_redshift_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Redshift connection found.", "/connect/redshift")


def no_snowflake_response(base_url: str) -> dict:
    return _no_connection(base_url, "No Snowflake connection found.", "/connect/snowflake")
