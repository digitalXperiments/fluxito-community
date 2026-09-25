"""Curated catalog of each connector's published third-party API rate limits.

This is a hand-maintained reference, not live usage tracking. Every entry is
sourced from the connector's official developer documentation (see ``docs_url``)
and carries a ``reviewed`` date and a ``confidence`` so the UI can be honest
about how firm each number is. Numbers drift — re-verify against the docs before
relying on them, and bump ``reviewed`` when you do.

The ``consumption_note`` on each connector translates the abstract quota into
"how fast does Fluxito itself burn it", grounded in how the app actually calls
the API (one dashboard card = one MCP tool call = usually one connector request;
a few connectors fan out — GTM's container summary is 5 calls). Aggressive Redis
caching (30s–600s TTLs) means repeated refreshes within a window cost nothing, so
these estimates are deliberately conservative ceilings, not alarms.

Each connector is keyed to the same ``has_*`` flags as
``GRANULAR_CONNECTOR_CATALOG`` in ``app.api.google_oauth_routes`` so the
"connected" set on Home and Project Settings never drifts from the connector
counter.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

# Date this catalog's numbers were last checked against the official docs.
REVIEWED = "2026-06-13"

# Categories (used to group connectors in the UI).
CAT_ANALYTICS = "Analytics"
CAT_ADVERTISING = "Advertising"
CAT_TAGGING = "Tag management"
CAT_SEARCH = "Search"
CAT_WAREHOUSE = "Data warehouse"
CAT_MARKETING = "Marketing automation"

# Confidence in the published numbers.
HIGH = "high"  # exact figures, first-party docs
MEDIUM = "medium"  # first-party docs but partial / dynamic / inherited limits
LOW = "low"  # not officially published; community-observed only


@dataclass(frozen=True)
class Limit:
    """A single documented quota or throttle."""

    name: str
    value: str
    window: str  # "per day", "per hour", "per second", "per 15 min", "concurrent", "—"
    scope: str  # "per property", "per project", "per app", "per ad account", …
    note: str = ""


@dataclass(frozen=True)
class Connector:
    """One connector's published rate-limit profile."""

    key: str
    name: str
    category: str
    docs_url: str
    # has_* attributes that mark this connector as connected for a project.
    flags: tuple[str, ...]
    headline: str  # short, card-friendly summary of the binding limit
    limits: tuple[Limit, ...]
    error_behavior: str
    headers: str  # rate-limit headers the API exposes, or "—"
    calls_per_card: str  # connector API calls per dashboard card / MCP read
    consumption_note: str  # grounded "how fast Fluxito burns it" estimate
    confidence: str
    usage: str = "≈1 call per card"  # short, card-facing "approx usage" line
    reviewed: str = REVIEWED


# ---------------------------------------------------------------------------
# The catalog. Ordered roughly by how commonly Fluxito projects use them.
# ---------------------------------------------------------------------------

CATALOG: tuple[Connector, ...] = (
    # ── Google Analytics 4 ────────────────────────────────────────────────
    Connector(
        key="ga4",
        name="Google Analytics 4",
        category=CAT_ANALYTICS,
        docs_url="https://developers.google.com/analytics/devguides/reporting/data/v1/quotas",
        flags=("has_ga4",),
        headline="200k tokens / property / day · 10 concurrent",
        usage="≈1 report per card · cached 2 min",
        limits=(
            Limit("Core tokens", "200,000", "per day", "per property", "Analytics 360: 2,000,000"),
            Limit("Core tokens", "40,000", "per hour", "per property", "Analytics 360: 400,000"),
            Limit(
                "Per-project-per-property tokens",
                "14,000",
                "per hour",
                "per property + project",
                "35% cap of the hourly budget; Analytics 360: 140,000",
            ),
            Limit("Concurrent requests", "10", "concurrent", "per property", "Analytics 360: 50"),
            Limit(
                "Token categories",
                "Core / Realtime / Funnel",
                "—",
                "independent meters",
                "each request also draws on up to 5 separate quota buckets",
            ),
        ),
        error_behavior=(
            "Exhausting a token bucket returns HTTP 429 RESOURCE_EXHAUSTED and blocks the "
            "property until the window resets. Fluxito retries with exponential backoff."
        ),
        headers="PropertyQuota in the response body (tokensPerDay, tokensPerHour, concurrentRequests, …)",
        calls_per_card="1",
        consumption_note=(
            "A typical dashboard card is one GA4 report costing a handful of variable 'tokens' "
            "(cost grows with date range, dimensions and which of the 5 buckets it hits). The "
            "limit you actually feel first is usually the 10 concurrent requests per property — a "
            "dashboard hydrates at most ~4 cards at once, well inside it. Reports are cached 120s, "
            "so rapid re-refreshes don't re-spend tokens."
        ),
        confidence=HIGH,
    ),
    # ── Google Tag Manager ────────────────────────────────────────────────
    Connector(
        key="gtm",
        name="Google Tag Manager",
        category=CAT_TAGGING,
        docs_url="https://developers.google.com/tag-platform/tag-manager/api/v2/limits-quotas",
        flags=("has_gtm",),
        headline="0.25 req/s · 10k requests / project / day",
        usage="≈5 calls per card · cached 5 min",
        limits=(
            Limit("Requests", "10,000", "per day", "per project", "resets at midnight PST"),
            Limit(
                "Request rate",
                "0.25 req/s (25 per 100s)",
                "per 100 sec",
                "per project",
                "burst ceiling — the real bottleneck",
            ),
        ),
        error_behavior=(
            "Exceeding quota returns HTTP 403 (not 429) with reason rateLimitExceeded / "
            "dailyLimitExceeded; no Retry-After header. Fluxito retries with backoff."
        ),
        headers="—",
        calls_per_card="5",
        consumption_note=(
            "One GTM container-summary card fans out to 5 API calls (tags, triggers, variables, "
            "workspaces and version headers in parallel). With only 0.25 requests/sec allowed per "
            "project, even a couple of GTM cards refreshing together can momentarily trip the burst "
            "limit — that, not the 10k/day, is GTM's real ceiling. Container data is cached 300s, "
            "which keeps you far under the daily cap."
        ),
        confidence=HIGH,
    ),
    # ── BigQuery ──────────────────────────────────────────────────────────
    Connector(
        key="bigquery",
        name="BigQuery",
        category=CAT_WAREHOUSE,
        docs_url="https://cloud.google.com/bigquery/quotas",
        flags=("has_bq",),
        headline="200 TiB scanned / project / day",
        usage="≈1 query per card · billed by bytes scanned",
        limits=(
            Limit(
                "On-demand query usage",
                "200 TiB",
                "per day",
                "per project",
                "configurable default for new on-demand projects (Sep 2025+); per-user unlimited",
            ),
            Limit("Concurrent interactive queries", "1,000", "queued", "per project / region"),
            Limit("Concurrent batch queries", "20,000", "queued", "per project / region"),
            Limit("Concurrent multi-statement queries", "1,000", "concurrent", "per project"),
        ),
        error_behavior=(
            "Queries beyond the queue limit are queued; usage beyond the daily bytes-scanned quota "
            "fails with quotaExceeded (HTTP 403). Billed by bytes scanned, not request count."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each BigQuery card / MCP query is one job. There's no per-second request cap that "
            "matters here — what you spend is bytes scanned against 200 TiB/day. A well-filtered "
            "dashboard query scans little; an unfiltered scan over a huge table can burn the daily "
            "quota in a handful of runs. Table schemas are cached 24h; query results are not."
        ),
        confidence=HIGH,
    ),
    # ── Google Ads ────────────────────────────────────────────────────────
    Connector(
        key="google_ads",
        name="Google Ads",
        category=CAT_ADVERTISING,
        docs_url="https://developers.google.com/google-ads/api/docs/best-practices/quotas",
        flags=("has_ads",),
        headline="15,000 operations / day (Basic access)",
        usage="≈1 query per card · cached 1 min",
        limits=(
            Limit(
                "Daily operations",
                "15,000",
                "per day",
                "per developer token",
                "Basic access; Standard access: unlimited",
            ),
            Limit(
                "Request rate",
                "variable QPS (Token Bucket)",
                "per second",
                "per customer ID + developer token",
            ),
        ),
        error_behavior=(
            "Daily quota exhausted → RESOURCE_EXHAUSTED; QPS exceeded → "
            "RESOURCE_TEMPORARILY_EXHAUSTED. Two independent meters — back off on both."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Google Ads card is one streamed query (search_stream) no matter how many rows "
            "return. At Basic access you share 15,000 operations/day across the whole developer "
            "token — heavy multi-account reporting can approach it; apply for Standard access for "
            "unlimited. Cached 60s."
        ),
        confidence=HIGH,
    ),
    # ── Search Console ────────────────────────────────────────────────────
    Connector(
        key="search_console",
        name="Search Console",
        category=CAT_SEARCH,
        docs_url="https://developers.google.com/webmaster-tools/limits",
        flags=("has_gsc",),
        headline="1,200 queries / min per site",
        limits=(
            Limit(
                "Search Analytics",
                "1,200 QPM",
                "per minute",
                "per site and per user",
                "project: 40,000 QPM / 30M per day",
            ),
            Limit(
                "URL Inspection",
                "2,000",
                "per day",
                "per site",
                "600 QPM per site; project: 10M per day",
            ),
            Limit("Other resources", "20 QPS / 200 QPM", "per minute", "per user", "project: 100M per day"),
        ),
        error_behavior=(
            "HTTP 429 rateLimitExceeded; a separate load quota can return HTTP 403. Back off and retry."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Search Console card is one query against a generous 1,200/min-per-site budget — "
            "you'd need sustained heavy querying to hit it. The tighter limit is URL inspection at "
            "2,000/day per site."
        ),
        confidence=HIGH,
    ),
    # ── Google Data Manager ───────────────────────────────────────────────
    Connector(
        key="data_manager",
        name="Google Data Manager",
        category=CAT_MARKETING,
        docs_url="https://developers.google.com/data-manager/api/quotas",
        flags=("has_data_manager",),
        headline="Subject to Google Cloud API quotas",
        limits=(
            Limit(
                "Data Manager API",
                "Per-project quota",
                "—",
                "Google Cloud project",
                "Review the current quota in Google Cloud Console.",
            ),
        ),
        error_behavior="HTTP 429 RESOURCE_EXHAUSTED; retry with exponential backoff.",
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Audience operations are direct Data Manager API calls; avoid repeated writes and "
            "retry quota failures with backoff."
        ),
        confidence=MEDIUM,
    ),
    # ── Meta Ads ──────────────────────────────────────────────────────────
    Connector(
        key="meta_ads",
        name="Meta Ads",
        category=CAT_ADVERTISING,
        docs_url="https://developers.facebook.com/docs/graph-api/overview/rate-limiting/",
        flags=("has_meta",),
        headline="Dynamic — scales with active ads & users",
        usage="≈1 call per ad account · cached 1 min",
        limits=(
            Limit("Platform (app) calls", "200 × daily active users", "rolling hour", "per app"),
            Limit(
                "Ads Insights (Standard)",
                "600 + 400 × active ads − 0.001 × user errors",
                "per hour",
                "per ad account",
            ),
            Limit(
                "Ads Insights (Advanced)",
                "190,000 + 400 × active ads − 0.001 × user errors",
                "per hour",
                "per ad account",
                "Advanced Access tier",
            ),
        ),
        error_behavior=(
            "Throttling surfaces in the X-Business-Use-Case-Usage header (per-account usage %, "
            "estimated_time_to_regain_access) and as error code 17 / 80000-series. Back off as "
            "usage nears 100%."
        ),
        headers="X-Business-Use-Case-Usage (call_count, total_time, estimated_time_to_regain_access)",
        calls_per_card="1",
        consumption_note=(
            "Each Meta card is one insights call per ad account (the SDK auto-paginates internally). "
            "The limit is dynamic and formula-based — bigger accounts with more active ads get a "
            "higher ceiling — so watch the X-Business-Use-Case-Usage header rather than a fixed "
            "number. Cached 60s."
        ),
        confidence=HIGH,
    ),
    # ── TikTok Ads ────────────────────────────────────────────────────────
    Connector(
        key="tiktok_ads",
        name="TikTok Ads",
        category=CAT_ADVERTISING,
        docs_url="https://business-api.tiktok.com/portal/docs/rate-limits-for-tto-api/v1.3",
        flags=("has_tiktok",),
        headline="Adaptive per-app QPS (≈10, up to 20)",
        limits=(
            Limit(
                "Overall request rate",
                "Adaptive QPS per app (~10 default, 20 advanced)",
                "per second",
                "per app",
                "TikTok allocates dynamically; no fixed published number",
            ),
            Limit(
                "Per-endpoint family",
                "Independent buckets",
                "—",
                "per app",
                "Reporting metered separately from Campaigns; exact figures unpublished",
            ),
        ),
        error_behavior=(
            "Returns HTTP 200 with error code 40100 in the body (NOT 429) — 'requests temporarily "
            "restricted'. Detect on the body code and back off exponentially."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each TikTok card is one API call. TikTok doesn't publish a fixed ceiling — it throttles "
            "per app dynamically and signals it via error code 40100 inside a 200 response. Start "
            "conservative and request the advanced tier from TikTok if you run heavy reporting."
        ),
        confidence=MEDIUM,
    ),
    # ── Snapchat Ads ──────────────────────────────────────────────────────
    Connector(
        key="snap_ads",
        name="Snapchat Ads",
        category=CAT_ADVERTISING,
        docs_url="https://developers.snap.com/api/marketing-api/Ads-API/rate-limits",
        flags=("has_snap",),
        headline="20 req/s per app · 10 req/s per token",
        limits=(
            Limit("App-level rate", "20 req/s", "per second", "per app", "average request volume"),
            Limit("Token-level rate", "10 req/s", "per second", "per access token", "average"),
        ),
        error_behavior=(
            "HTTP 429 Too Many Requests. No documented Retry-After or X-RateLimit headers — lower "
            "your request rate if 429s recur."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Snapchat card is one call. The ceiling is 20 requests/sec per app (10/sec per "
            "access token), measured as an average — generous for dashboards, which fire only a "
            "handful of cards at once. Back off on repeated 429s."
        ),
        confidence=HIGH,
    ),
    # ── LinkedIn Ads ──────────────────────────────────────────────────────
    Connector(
        key="linkedin_ads",
        name="LinkedIn Ads",
        category=CAT_ADVERTISING,
        docs_url="https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/rate-limits",
        flags=("has_linkedin",),
        headline="45M metric values / 5 min (Reporting)",
        limits=(
            Limit(
                "adAnalytics data throttle",
                "45,000,000 metric values",
                "per 5 min",
                "per application",
                "metric values = metrics × records; the only published hard number",
            ),
            Limit(
                "Application calls",
                "Not published",
                "per day (UTC)",
                "per application",
                "varies by endpoint; visible only in the Developer Portal",
            ),
            Limit(
                "Member calls",
                "Not published",
                "per day (UTC)",
                "per member",
                "varies by endpoint",
            ),
            Limit("Response size", "15,000 elements", "per response", "—", "adAnalytics has no pagination"),
        ),
        error_behavior=(
            "HTTP 429 TOO_MANY_REQUESTS. Daily quotas are visible only in the Developer Portal "
            "Analytics tab; admins are emailed at 75% of the app quota. No Retry-After header."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each LinkedIn card is one adAnalytics call. LinkedIn deliberately doesn't publish daily "
            "call quotas — they vary per endpoint and are visible only in your Developer Portal. The "
            "one hard limit is 45M metric values per 5-minute window, which big reports (many "
            "metrics × many rows) can hit."
        ),
        confidence=MEDIUM,
    ),
    # ── Pinterest Ads ─────────────────────────────────────────────────────
    Connector(
        key="pinterest_ads",
        name="Pinterest Ads",
        category=CAT_ADVERTISING,
        docs_url="https://developers.pinterest.com/docs/reference/rate-limits/",
        flags=("has_pinterest",),
        headline="300 analytics calls / min per user (Standard)",
        limits=(
            Limit("Overall (Standard)", "100 req/s", "per second", "per user per app"),
            Limit(
                "Ads analytics (Standard)",
                "300",
                "per minute",
                "per user per app",
                "Trial access: 1,000 per day per app",
            ),
            Limit("Org analytics (Standard)", "60", "per minute", "per user per app"),
            Limit(
                "Trial access (all endpoints)",
                "1,000",
                "per day",
                "per app",
                "until Standard access is approved",
            ),
        ),
        error_behavior=(
            "Usage is exposed via x-ratelimit-limit / x-ratelimit-remaining / x-ratelimit-reset "
            "headers (HTTP 429 is conventional but not stated on the page)."
        ),
        headers="x-ratelimit-limit / -remaining / -reset",
        calls_per_card="1",
        consumption_note=(
            "Each Pinterest card is one call. On Trial access you only get 1,000 calls/day per app — "
            "easy to exhaust with dashboards; on Standard access the analytics limit jumps to "
            "300/min per user. Pace off the x-ratelimit-remaining header."
        ),
        confidence=HIGH,
    ),
    # ── X (Twitter) Ads ───────────────────────────────────────────────────
    Connector(
        key="x_ads",
        name="X Ads",
        category=CAT_ADVERTISING,
        docs_url="https://docs.x.com/x-ads-api/fundamentals/rate-limiting",
        flags=("has_x",),
        headline="250 analytics calls / 15 min",
        limits=(
            Limit("Analytics (synchronous)", "250", "per 15 min", "per user token", "the tightest read"),
            Limit("Core entity reads (GET)", "10,000", "per 15 min", "per ad account / endpoint"),
            Limit("Writes (POST/PUT/DELETE)", "450", "per minute", "per user token"),
        ),
        error_behavior=(
            "HTTP 429. Two header families: X-Rate-Limit-* (user token) and X-Account-Rate-Limit-* "
            "(ad account); reset is an epoch timestamp. Auth is OAuth 1.0a."
        ),
        headers="X-Rate-Limit-* and X-Account-Rate-Limit-*",
        calls_per_card="1",
        consumption_note=(
            "Each X Ads analytics card is one call against a 250-per-15-min budget — the tightest of "
            "the reads. Heavy synchronous-analytics dashboards hit this quickly; prefer fewer, "
            "broader queries and pace off X-Account-Rate-Limit-Remaining."
        ),
        confidence=HIGH,
    ),
    # ── Reddit Ads ────────────────────────────────────────────────────────
    Connector(
        key="reddit_ads",
        name="Reddit Ads",
        category=CAT_ADVERTISING,
        docs_url="https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki",
        flags=("has_reddit",),
        headline="100 queries / min per OAuth client",
        limits=(
            Limit(
                "OAuth clients",
                "100 QPM",
                "per minute",
                "per OAuth client ID",
                "averaged over 10 min; legacy wiki still says 60/min",
            ),
            Limit("Unauthenticated", "10 QPM", "per minute", "per client"),
        ),
        error_behavior=(
            "HTTP 429. Headers X-Ratelimit-Used / -Remaining / -Reset (seconds to window end). "
            "No Retry-After."
        ),
        headers="X-Ratelimit-Used / -Remaining / -Reset",
        calls_per_card="1",
        consumption_note=(
            "Each Reddit card is one call against ~100 queries/min per OAuth client, shared across "
            "the app — the Ads API inherits Reddit's standard OAuth limit. Pace off the "
            "X-Ratelimit-Remaining header."
        ),
        confidence=MEDIUM,
    ),
    # ── Apple Search Ads ──────────────────────────────────────────────────
    Connector(
        key="apple_ads",
        name="Apple Search Ads",
        category=CAT_ADVERTISING,
        docs_url="https://developer.apple.com/documentation/apple_ads",
        flags=("has_apple",),
        headline="Dynamic — surfaced via X-Rate-Limit header",
        limits=(
            Limit(
                "Request rate",
                "Adaptive (~300/min observed)",
                "per hour",
                "per organization",
                "Apple does not publish a fixed number",
            ),
        ),
        error_behavior=(
            "HTTP 429 when exceeded. Every response carries an X-Rate-Limit header (limit + "
            "remaining for the current window)."
        ),
        headers="X-Rate-Limit",
        calls_per_card="1",
        consumption_note=(
            "Each Apple Search Ads card is one call. Apple doesn't publish a fixed ceiling — it "
            "reports the live limit in the X-Rate-Limit header (developers observe ~300 "
            "requests/min). Pace off that header rather than a hardcoded number."
        ),
        confidence=LOW,
    ),
    # ── Bing Webmaster Tools ──────────────────────────────────────────────
    Connector(
        key="bing_webmaster",
        name="Bing Webmaster Tools",
        category=CAT_SEARCH,
        docs_url="https://www.bing.com/webmasters/help/",
        flags=("has_bing",),
        headline="10,000 URL submissions / day per site",
        limits=(
            Limit(
                "URL submission",
                "10,000",
                "per day",
                "per site",
                "100/day for newly verified sites; resets at midnight GMT",
            ),
            Limit("Batch size", "500 URLs", "per request", "—"),
        ),
        error_behavior="HTTP 429 with a Retry-After header when throttled. GetUrlSubmissionQuota reports remaining daily quota.",
        headers="Retry-After",
        calls_per_card="1",
        consumption_note=(
            "Bing's published quota is for URL submission (10,000/day per verified site, only "
            "100/day until the site is established). Query/analytics reads are lighter. Check "
            "GetUrlSubmissionQuota for remaining headroom."
        ),
        confidence=MEDIUM,
    ),
    # ── Adobe Analytics ───────────────────────────────────────────────────
    Connector(
        key="adobe_analytics",
        name="Adobe Analytics",
        category=CAT_ANALYTICS,
        docs_url="https://developer.adobe.com/analytics-apis/docs/2.0/guides/faq",
        flags=("has_adobe_analytics",),
        headline="12 requests / 6 seconds per user",
        limits=(
            Limit("Throttle", "12 requests / 6 sec (~120/min)", "per 6 sec", "per user"),
            Limit("Request timeout", "60 seconds", "per request", "—"),
        ),
        error_behavior=(
            "HTTP 429 with error code 429050 ('Too many requests'). No Retry-After; back off "
            "exponentially. Adobe advises against polling faster than ~30 min."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Adobe Analytics card is one report request, throttled to 12 per 6 seconds per "
            "user. A dashboard hydrates ~4 cards at once, comfortably under it; many Adobe cards "
            "refreshing together could trip the throttle briefly."
        ),
        confidence=HIGH,
    ),
    # ── Adobe Launch / Reactor ────────────────────────────────────────────
    Connector(
        key="adobe_launch",
        name="Adobe Launch (Reactor)",
        category=CAT_TAGGING,
        docs_url="https://experienceleague.adobe.com/en/docs/experience-platform/tags/api/overview",
        flags=("has_adobe_launch",),
        headline="Not officially published",
        limits=(
            Limit(
                "Request rate",
                "Not documented by Adobe (~120/min per integration observed)",
                "per minute",
                "per integration",
                "unofficial; no published limit",
            ),
        ),
        error_behavior=(
            "Reportedly HTTP 429 with Retry-After; the reactor SDK auto-throttles. Adobe publishes "
            "no official limit."
        ),
        headers="Retry-After (reported)",
        calls_per_card="1",
        consumption_note=(
            "Adobe doesn't publish a Reactor/Launch rate limit. The SDK Fluxito uses backs off on "
            "429s automatically. Treat throughput as modest (~2 req/s observed) and rely on backoff."
        ),
        confidence=LOW,
    ),
    # ── Marketo ───────────────────────────────────────────────────────────
    Connector(
        key="marketo",
        name="Marketo Engage",
        category=CAT_MARKETING,
        docs_url="https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/marketo-integration-best-practices",
        flags=("has_marketo",),
        headline="50,000 calls / day · 100 / 20s · 10 concurrent",
        limits=(
            Limit(
                "Daily quota",
                "50,000",
                "per day",
                "per instance",
                "default; purchasable higher; resets at midnight CST",
            ),
            Limit(
                "Burst rate",
                "100 calls / 20 sec",
                "per 20 sec",
                "per instance",
                "Adobe recommends self-limiting to 50 / 20s",
            ),
            Limit("Concurrency", "10 concurrent calls", "concurrent", "per instance"),
        ),
        error_behavior=(
            "Vendor error codes in the body: 606 (rate, 100/20s), 607 (daily quota), 615 (10 "
            "concurrent). Back off on 606/615."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Marketo card is one call against 50,000/day shared by the whole instance — "
            "generous, but the 100-calls-per-20-seconds burst and 10-concurrent limits bite first "
            "under heavy parallel use."
        ),
        confidence=HIGH,
    ),
    # ── Braze ─────────────────────────────────────────────────────────────
    Connector(
        key="braze",
        name="Braze",
        category=CAT_MARKETING,
        docs_url="https://www.braze.com/docs/api/overview/",
        flags=("has_braze",),
        headline="250,000 requests/hour (default) · ~10 req/s burst",
        usage="≈1 call per card",
        limits=(
            Limit(
                "REST API hourly quota",
                "250,000",
                "per hour",
                "per dashboard cluster",
                "default; higher on request; resets on the hour",
            ),
            Limit(
                "Burst rate",
                "~10 req/s",
                "per second",
                "per dashboard cluster",
                "soft ceiling; 429 beyond sustainable rate",
            ),
        ),
        error_behavior=(
            "HTTP 429 Too Many Requests; Retry-After header returned. The hourly quota resets at "
            "the top of each hour."
        ),
        headers="Retry-After, X-RateLimit-*",
        calls_per_card="1",
        consumption_note=(
            "Each Braze card is one REST API call. The 250,000/hour cluster-wide pool is generous, "
            "but bursts exceeding ~10/s also trigger 429s. Dashboard usage is typically well within "
            "these limits."
        ),
        confidence=HIGH,
    ),
    # ── Moengage ───────────────────────────────────────────────────────────
    Connector(
        key="moengage",
        name="Moengage",
        category=CAT_MARKETING,
        docs_url="https://docs.moengage.com/",
        flags=("has_moengage",),
        headline="1,000 req/min · 1,000,000 req/day (default)",
        usage="≈1 call per card",
        limits=(
            Limit(
                "REST API rate",
                "1,000 req/min",
                "per minute",
                "per app",
                "plan-dependent; some plans allow more",
            ),
            Limit(
                "Daily quota",
                "1,000,000 req/day",
                "per day",
                "per app",
                "default; lower on Starter plans",
            ),
        ),
        error_behavior=("HTTP 429 Too Many Requests with Retry-After; respect per-minute and daily caps."),
        headers="Retry-After, X-RateLimit-Remaining",
        calls_per_card="1",
        consumption_note=(
            "Each MoEngage card is one REST API call. The 1,000/min rate limit is the binding "
            "constraint under parallel dashboards; the 1,000,000/day quota is rarely hit by "
            "dashboard usage alone."
        ),
        confidence=MEDIUM,
    ),
    # ── Amplitude ─────────────────────────────────────────────────────────
    Connector(
        key="amplitude",
        name="Amplitude",
        category=CAT_ANALYTICS,
        docs_url="https://amplitude.com/docs/apis/analytics/dashboard-rest",
        flags=("has_amplitude",),
        headline="5 concurrent + cost-based (Dashboard REST)",
        usage="≈1 query per card · metered by cost",
        limits=(
            Limit("Dashboard REST concurrency", "5 concurrent", "concurrent", "per project"),
            Limit(
                "Dashboard REST cost",
                "1,000 cost / 5 min · 108,000 cost / hour",
                "per 5 min / hour",
                "per project",
                "cost = days × conditions × query-type weight",
            ),
            Limit("User activity / search", "10 concurrent · 360 queries/hour", "per hour", "per project"),
            Limit("Export API", "365 days max · 4 GB max", "per request", "per project"),
            Limit("HTTP V2 ingest", "1,000 events/s (Starter)", "per second", "per project"),
        ),
        error_behavior=(
            "Dashboard REST: HTTP 429 (the body names the limit). Export: 400 (>4 GB) / 504 "
            "(timeout). HTTP V2: 413 (payload) / 429 (device throttle)."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Amplitude card is one Dashboard REST query, but Amplitude meters by 'cost' "
            "(days × conditions × query-type weight), capped at 1,000 cost per 5 min. Wide date "
            "ranges and complex segmentation cost more — a few heavy queries hit the window faster "
            "than the 5-concurrent limit suggests."
        ),
        confidence=HIGH,
    ),
    # ── Branch ────────────────────────────────────────────────────────────
    Connector(
        key="branch",
        name="Branch",
        category=CAT_ADVERTISING,
        docs_url="https://help.branch.io/using-branch/docs/branch-dashboard-api",
        flags=("has_branch",),
        headline="~50 req/min · 1 concurrent export per app",
        usage="≈1 call per card",
        limits=(
            Limit("Dashboard REST", "~50 req/min", "per minute", "per account"),
            Limit("Exports concurrency", "1 concurrent", "concurrent", "per app"),
            Limit("Webhook delivery", "100/s", "per second", "per account"),
        ),
        error_behavior=(
            "Rate limit returns 429. Export requests beyond 1 concurrent queue or reject. "
            "Use backoff on 429."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Branch card makes one Dashboard REST call. Exports are queued 1-at-a-time per app; "
            "heavy card usage hits the 50/min limit before export concurrency."
        ),
        confidence=HIGH,
    ),
    # ── AppsFlyer ─────────────────────────────────────────────────────────
    Connector(
        key="appsflyer",
        name="AppsFlyer",
        category=CAT_ADVERTISING,
        docs_url="https://dev.appsflyer.com/hc/en-us/articles/207034356",
        flags=("has_appsflyer",),
        headline="100 req/min (master) · 60 reports/day (pull)",
        usage="≈1 call per card",
        limits=(
            Limit("Master API", "100 req/min", "per minute", "per account"),
            Limit("Pull API reports", "60 reports/day", "per day", "per account"),
            Limit("Raw data reports", "1 active report", "concurrent", "per app"),
        ),
        error_behavior=(
            "Master API 429 on rate; Pull API returns quota headers. Raw reports queue when "
            "1-per-app is busy."
        ),
        headers="X-RateLimit-Remaining, X-RateLimit-Reset",
        calls_per_card="1",
        consumption_note=(
            "Each AppsFlyer card is one Master API call. Pull API reports are expensive (60/day); "
            "avoid per-card raw pulls."
        ),
        confidence=HIGH,
    ),
    # ── Adjust ────────────────────────────────────────────────────────────
    Connector(
        key="adjust",
        name="Adjust",
        category=CAT_ADVERTISING,
        docs_url="https://help.adjust.com/en/article/reports-service-api",
        flags=("has_adjust",),
        headline="~170 req/min · 1 concurrent pivot",
        usage="≈1 call per card",
        limits=(
            Limit("Report Service", "~170 req/min", "per minute", "per token"),
            Limit("Pivot reports", "1 concurrent", "concurrent", "per token"),
            Limit("CSV export size", "100K rows", "per request", "per request"),
        ),
        error_behavior=("429 on rate limit. Pivot concurrency returns 409/429; queue or reduce concurrency."),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Adjust card is one Report Service call. Pivot cards consume the single concurrent "
            "slot; CSV exports are large but infrequent."
        ),
        confidence=HIGH,
    ),
    # ── Mixpanel ──────────────────────────────────────────────────────────
    Connector(
        key="mixpanel",
        name="Mixpanel",
        category=CAT_ANALYTICS,
        docs_url="https://developer.mixpanel.com/docs/query-apis",
        flags=("has_mixpanel",),
        headline="6 concurrent / 60 req/min per project",
        usage="≈1 query per card · cached",
        limits=(
            Limit(
                "Query API",
                "6 concurrent; 60 req/min per IP",
                "concurrent / per minute",
                "per project",
                "429 on breach; Retry-After header",
            ),
            Limit(
                "Ingest API",
                "2 GB / hour batch",
                "per hour",
                "per project",
                "events dropped beyond cap",
            ),
            Limit(
                "Engage API",
                "6 concurrent; 60 req/min per IP",
                "concurrent / per minute",
                "per project",
                "429 with Retry-After",
            ),
        ),
        error_behavior="HTTP 429 Too Many Requests; back off using Retry-After.",
        headers="Retry-After",
        calls_per_card="1",
        consumption_note="Each Mixpanel card is one Query/Engage call. The 6-concurrent and 60/min ceilings are the practical limits for dashboards.",
        confidence=HIGH,
    ),
    # ── PostHog ───────────────────────────────────────────────────────────
    Connector(
        key="posthog",
        name="PostHog",
        category=CAT_ANALYTICS,
        docs_url="https://posthog.com/docs/api",
        flags=("has_posthog",),
        headline="Instance-dependent (Cloud Free ~240/min)",
        usage="≈1 query per card · cached",
        limits=(
            Limit(
                "REST API",
                "240 req/min (Free); higher on paid",
                "per minute",
                "per project",
                "Cloud plans vary; self-hosted configurable",
            ),
            Limit(
                "Ingestion API",
                "1 M events / month (Free)",
                "per month",
                "per project",
                "events rejected beyond plan cap",
            ),
        ),
        error_behavior="HTTP 429 Too Many Requests; respect Retry-After.",
        headers="Retry-After",
        calls_per_card="1",
        consumption_note="Each PostHog card is one REST call. Free tier caps at ~240/min and 1 M events/month ingestion.",
        confidence=MEDIUM,
    ),
    # ── Amazon Redshift ───────────────────────────────────────────────────
    Connector(
        key="redshift",
        name="Amazon Redshift",
        category=CAT_WAREHOUSE,
        docs_url="https://docs.aws.amazon.com/redshift/latest/mgmt/amazon-redshift-limits.html",
        flags=("has_redshift",),
        headline="500–2,000 connections · ~50 query slots",
        usage="≈1 query per card · pooled connection",
        limits=(
            Limit(
                "Concurrent connections",
                "500 (dc2.large) – 2,000 (RA3 / Serverless)",
                "concurrent",
                "per cluster",
                "node-type dependent",
            ),
            Limit("WLM query concurrency", "50 slots max (default queue 5)", "concurrent", "per cluster"),
            Limit(
                "Concurrency-scaling clusters",
                "10",
                "concurrent",
                "per account / region",
                "adjustable",
            ),
        ),
        error_behavior=(
            "Over the connection ceiling → new connections refused. Over WLM concurrency → queries "
            "queue until a slot frees (or scale out). This is concurrency, not requests-per-second."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Redshift card is one SQL query over a pooled connection. There's no API rate "
            "limit — the constraints are concurrent connections (500–2,000 by node type) and WLM "
            "query slots (~50). Many simultaneous dashboard queries queue rather than fail."
        ),
        confidence=HIGH,
    ),
    # ── Snowflake ─────────────────────────────────────────────────────────
    Connector(
        key="snowflake",
        name="Snowflake",
        category=CAT_WAREHOUSE,
        docs_url="https://docs.snowflake.com/en/user-guide/performance-query-warehouse-max-concurrency",
        flags=("has_snowflake",),
        headline="8 concurrent queries / warehouse (default)",
        usage="≈1 query per card · 1 warehouse slot",
        limits=(
            Limit(
                "Max concurrency",
                "8 (default MAX_CONCURRENCY_LEVEL)",
                "concurrent",
                "per warehouse",
                "configurable; multi-cluster warehouses auto-scale",
            ),
            Limit(
                "Statement timeout",
                "172,800 s (48h, default)",
                "per statement",
                "per warehouse / account",
                "configurable",
            ),
            Limit("Queued timeout", "0 (no queue timeout by default)", "—", "per warehouse", "configurable"),
        ),
        error_behavior=(
            "Beyond the concurrency level, queries queue (or a multi-cluster warehouse spins up more "
            "clusters). Running statements cancel after STATEMENT_TIMEOUT. Not requests-per-second."
        ),
        headers="—",
        calls_per_card="1",
        consumption_note=(
            "Each Snowflake card is one SQL statement. No API rate limit — concurrency is governed "
            "by warehouse settings (8 running queries by default before queuing). Heavy concurrent "
            "dashboards either queue or, on multi-cluster warehouses, scale out (and cost more)."
        ),
        confidence=HIGH,
    ),
)

_BY_KEY: dict[str, Connector] = {c.key: c for c in CATALOG}


def by_key(key: str) -> Connector | None:
    """Return the connector with ``key``, or None."""
    return _BY_KEY.get(key)


def connected_keys(flags: object) -> set[str]:
    """Connector keys that ``flags`` (a has_* namespace/context) marks connected.

    ``flags`` is any object carrying ``has_*`` attributes — the same flags object
    the Home and Settings routes already build. A connector counts as connected
    if ANY of its ``flags`` attributes is truthy.
    """
    return {c.key for c in CATALOG if any(getattr(flags, f, False) for f in c.flags)}


def partition(connected: set[str]) -> tuple[list[Connector], list[Connector]]:
    """Split the catalog into (connected, available), preserving catalog order."""
    conn = [c for c in CATALOG if c.key in connected]
    avail = [c for c in CATALOG if c.key not in connected]
    return conn, avail


def to_view(connectors: list[Connector]) -> list[dict]:
    """Render connectors to plain JSON-friendly dicts for template consumption."""
    views = []
    for c in connectors:
        d = dataclasses.asdict(c)
        # asdict preserves tuple fields as tuples; hand the template plain lists.
        d["flags"] = list(d["flags"])
        d["limits"] = [dict(limit) for limit in d["limits"]]
        views.append(d)
    return views
