"""
Application State Singletons

Connectors and per-session user context are stored here so tool handlers
can import them directly without needing a FastAPI request context.

In mcp 1.4.x, FastMCP tool functions must not have unannotated parameters
(they get treated as Claude-provided tool arguments). Instead, tools import
from this module.
"""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from app.auth.google_token_manager import GoogleTokenManager
    from app.auth.mcp_session_manager import ProjectContext, UserContext
    from app.connectors.bigquery import BigQueryConnector
    from app.connectors.data_manager import DataManagerConnector
    from app.connectors.ga4 import GA4Connector
    from app.connectors.google_ads import GoogleAdsConnector
    from app.connectors.gtm import GTMConnector
    from app.connectors.search_console import SearchConsoleConnector

# ---------------------------------------------------------------------------
# Connector singletons — set in main.py lifespan
# ---------------------------------------------------------------------------
ga4_connector: Optional["GA4Connector"] = None
data_manager_connector: Optional["DataManagerConnector"] = None
gtm_connector: Optional["GTMConnector"] = None
ads_connector: Optional["GoogleAdsConnector"] = None
search_console_connector: Optional["SearchConsoleConnector"] = None
bq_connector: Optional["BigQueryConnector"] = None
meta_connector: Any = None
tiktok_connector: Any = None
snap_connector: Any = None
# New platform connectors
linkedin_connector: Any = None
pinterest_connector: Any = None
x_connector: Any = None
reddit_connector: Any = None
bing_connector: Any = None
apple_connector: Any = None
amplitude_connector: Any = None
mixpanel_connector: Any = None
posthog_connector: Any = None
adobe_analytics_connector: Any = None
adobe_launch_connector: Any = None
adobe_marketo_connector: Any = None
redshift_connector: Any = None
snowflake_connector: Any = None
branch_connector: Any = None
appsflyer_connector: Any = None
adjust_connector: Any = None
braze_connector: Any = None
moengage_connector: Any = None
token_manager: Optional["GoogleTokenManager"] = None

# Typed as Any so static analysers (basedpyright) don't flag these as
# "Object of type None cannot be called" — they are set at runtime in
# the FastAPI lifespan hook before any request can reach them.
redis_client: Any = None
db_session_factory: Any = None

# ---------------------------------------------------------------------------
# Per-MCP-request user context
# Set by the /mcp Streamable HTTP route handler for the duration of each
# request. Tools read this to know who is making the request.
# ---------------------------------------------------------------------------
current_user_ctx: ContextVar[Optional["UserContext"]] = ContextVar("current_user_ctx", default=None)

# ---------------------------------------------------------------------------
# Per-MCP-session active project context
# Set by the ``set_active_project`` tool (or auto-selected when user has
# only one project). Tools read this to scope all operations to a project.
# ---------------------------------------------------------------------------
current_project_ctx: ContextVar[Optional["ProjectContext"]] = ContextVar("current_project_ctx", default=None)

# Human-readable name of the MCP client making the current call (e.g. "Claude",
# "ChatGPT", "Cursor"). Set by the MCP auth middleware alongside current_user_ctx.
current_client_name_ctx: ContextVar[str | None] = ContextVar("current_client_name_ctx", default=None)
# Scopes of the MCP token behind the current call (e.g. ["read", "write"]).
# None = not an MCP-token call (Ask, dashboard refresh…) → no scope limit.
current_token_scopes_ctx: ContextVar[list[str] | None] = ContextVar("current_token_scopes_ctx", default=None)

# ---------------------------------------------------------------------------
# Per-tool-call activity metadata
# Set by the billing hook in registry.py so log_usage() can record status
# and source client for the activity log.
# ---------------------------------------------------------------------------
tool_call_status_ctx: ContextVar[str | None] = ContextVar("tool_call_status_ctx", default=None)
tool_call_source_ctx: ContextVar[str | None] = ContextVar("tool_call_source_ctx", default=None)
