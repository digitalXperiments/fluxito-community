# Fluxito Community

### An open-source MCP server for marketing analytics.

**Connect 27 marketing and analytics platforms once, then work with them from any AI — plus tracking audits built in.**

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/-Self--host%20with%20Docker-0f766e?style=for-the-badge&logo=docker&logoColor=white" alt="Self-host with Docker"></a>
  <a href="docs/tutorials/connect-ai-mcp.md"><img src="https://img.shields.io/badge/-Connect%20your%20AI-6f42c1?style=for-the-badge" alt="Connect your AI"></a>
  <a href="https://fluxito.app"><img src="https://img.shields.io/badge/-Fluxito%20Cloud-2F5BF4?style=for-the-badge" alt="Fluxito Cloud"></a>
</p>

<p align="center">
  <a href="https://github.com/digitalXperiments/fluxito-community/releases/latest"><img src="https://img.shields.io/github/v/release/digitalXperiments/fluxito-community?label=release&color=2F5BF4" alt="Release"></a>
  <a href="#connecting-your-platforms"><img src="https://img.shields.io/badge/platforms-27-orange.svg" alt="27 platforms"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="Apache 2.0"></a>
</p>

---

## What it is

Fluxito Community gives your AI real, authenticated access to your marketing stack through the [Model Context Protocol](https://modelcontextprotocol.io). Point Claude, ChatGPT, Cursor, Windsurf, or anything else that speaks MCP at your Fluxito server, and it can read GA4, audit GTM, compare ad spend, or query your warehouse — with your permissions, on your infrastructure.

| | |
|---|---|
| 🔌 **MCP server** | One `/mcp` endpoint for 27 platforms: GA4, GTM, Google/Meta/TikTok/LinkedIn/Snap/X/Reddit/Pinterest/Apple ads, Search Console, Bing, BigQuery, Snowflake, Redshift, Amplitude, Mixpanel, PostHog, Adobe, Braze, MoEngage and more |
| 🔍 **Audit** | Tag QA against 21 platform rule books, live tag tests, and scheduled test flows with email/Slack alerts |
| 📚 **Context** | A KPI library and business context your AI reads, so answers use your metrics and terminology |
| 💬 **Chat** | A simple built-in chat that uses your own OpenAI-compatible API key (OpenAI, OpenRouter, or any compatible endpoint). Read-only: it can look at your connected platforms but never change them |
| 👥 **Teams** | Projects, members, roles (RBAC), per-project OAuth apps, and an activity log of every AI tool call |

**Want more?** [Fluxito Cloud](https://fluxito.app) adds tracking plans, GTM implementation, hosted dashboards, scheduled automations and more.

---

## Install

### Docker (recommended — no clone)

```bash
mkdir fluxito && cd fluxito
curl -O https://raw.githubusercontent.com/digitalXperiments/fluxito-community/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/digitalXperiments/fluxito-community/main/.env.example

# One-click in-app updates (optional but recommended)
echo "UPDATER_TOKEN=$(openssl rand -hex 32)" >> .env

# Pin secrets so encrypted tokens survive restarts/updates
docker pull ghcr.io/digitalxperiments/fluxito-community:latest
echo "APP_SECRET_KEY=$(docker run --rm ghcr.io/digitalxperiments/fluxito-community python -c 'import secrets; print(secrets.token_hex(32))')" >> .env
echo "TOKEN_ENCRYPTION_KEY=$(docker run --rm ghcr.io/digitalxperiments/fluxito-community python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env

docker compose up -d
```

Open **http://localhost:8000**. Update anytime from **Admin → Updates**, or:

```bash
docker compose pull && docker compose up -d
```

### From source (track `main` / fork)

```bash
git clone https://github.com/digitalXperiments/fluxito-community.git
cd fluxito-community
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Optional in `.env` so you don't repeat both compose files:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml
```

---

## Updating

**Release images:** super-admin **Admin → Updates → Update now** (pulls image, recreates container, rolls back on failed health). Needs `UPDATER_TOKEN` in `.env`. Without it, use `docker compose pull && docker compose up -d`.

**Source installs:**

```bash
git pull
docker compose up -d --build   # or with both compose files if COMPOSE_FILE isn't set
```

Migrations run on startup (`alembic upgrade head`). Named volumes for Postgres/Redis persist — **never** `docker compose down -v` to update.

**Air-gapped:** set `UPDATE_CHECKS_ENABLED=false` to stop outbound GitHub version checks.

---

## First run

Rough first-time cost: **15–25 min** for Google OAuth (one app unlocks five platforms), then **~5 min** to connect accounts + AI.

1. Open http://localhost:8000 → create the first admin (email + password; no email verification on self-host).
2. **Admin → OAuth Apps** → follow **[Google Cloud Setup](docs/tutorials/google-cloud-setup.md)** (GA4, GTM, Ads, Search Console, BigQuery).
3. **/connect** → authorize the Google accounts you want the AI to use.
4. Connect an MCP client (or use the built-in **Chat** with your own API key under **Settings → AI providers**). See **[Connect AI with MCP](docs/tutorials/connect-ai-mcp.md)** and the section below.

---

## Connecting your platforms

Start with Google when you can — one OAuth client unlocks five surfaces.

| Platform | Tutorial |
|---|---|
| **Google** (GA4, GTM, Ads, Search Console, BigQuery) | [google-cloud-setup.md](docs/tutorials/google-cloud-setup.md) |
| Google Analytics 4 | [google-analytics-4.md](docs/tutorials/google-analytics-4.md) |
| Google Tag Manager | [google-tag-manager.md](docs/tutorials/google-tag-manager.md) |
| Google Ads | [google-ads.md](docs/tutorials/google-ads.md) |
| Search Console | [search-console.md](docs/tutorials/search-console.md) |
| Bing Webmaster Tools | [bing-webmaster.md](docs/tutorials/bing-webmaster.md) |
| BigQuery | [bigquery.md](docs/tutorials/bigquery.md) |
| Meta Ads | [meta-ads.md](docs/tutorials/meta-ads.md) |
| TikTok Ads | [tiktok-ads.md](docs/tutorials/tiktok-ads.md) |
| LinkedIn Ads | [linkedin-ads.md](docs/tutorials/linkedin-ads.md) |
| Pinterest Ads | [pinterest-ads.md](docs/tutorials/pinterest-ads.md) |
| Snap Ads | [snap-ads.md](docs/tutorials/snap-ads.md) |
| X Ads | [x-ads.md](docs/tutorials/x-ads.md) |
| Reddit Ads | [reddit-ads.md](docs/tutorials/reddit-ads.md) |
| Apple Search Ads | [apple-ads.md](docs/tutorials/apple-ads.md) |
| Snowflake | [snowflake.md](docs/tutorials/snowflake.md) |
| Redshift | [redshift.md](docs/tutorials/redshift.md) |
| Amplitude | [amplitude.md](docs/tutorials/amplitude.md) |
| Adobe Analytics | [adobe-analytics.md](docs/tutorials/adobe-analytics.md) |
| Adobe Launch | [adobe-launch.md](docs/tutorials/adobe-launch.md) |
| Adobe Marketo Engage | [adobe-marketo.md](docs/tutorials/adobe-marketo.md) |
| Mixpanel | [mixpanel.md](docs/tutorials/mixpanel.md) |
| PostHog | [posthog.md](docs/tutorials/posthog.md) |
| Adjust | [adjust.md](docs/tutorials/adjust.md) |
| AppsFlyer | [appsflyer.md](docs/tutorials/appsflyer.md) |
| Branch | [branch.md](docs/tutorials/branch.md) |
| Braze | [braze.md](docs/tutorials/braze.md) |
| MoEngage | [moengage.md](docs/tutorials/moengage.md) |

Credentials are encrypted at rest and managed in **Admin → OAuth Apps** after the first admin exists — not via `.env`.

---

## Team access (RBAC)

Projects are multi-user. One role model covers **MCP tools and the web UI**.

| Tier | Access |
|---|---|
| **Owner** | Everything + structural actions (transfer ownership, delete project) |
| **Admin** | Full tools/connections; manage members and roles |
| **Member** | No access until assigned role(s) |

Custom roles (**Settings → User Roles**):

- **Tools by domain** — analytics, audience, tag manager, marketing, SEO, warehouse, knowledge, analysis (read/write)
- **Connections by provider** — e.g. GA4 + Search Console, but not Ads

Members can hold multiple roles (union of permissions). Ungranted tools are hidden from `tools/list` and re-checked at execution. RBAC is **off by default** per project.

---

## Configuration

Two surfaces:

1. **`.env` / environment** — bootstrap only (before DB / first login)
2. **Web UI** — OAuth apps, system settings, MCP, email, rate limits, etc.

### Bootstrap env vars

| Variable | Purpose |
|---|---|
| `APP_SECRET_KEY` | Signs session cookies (≥32 chars). Rotating logs everyone out. |
| `TOKEN_ENCRYPTION_KEY` | Fernet key for OAuth/API credentials in DB. **Losing it orphans tokens.** |
| `DATABASE_URL` | Postgres |
| `REDIS_URL` | Redis |
| `APP_BASE_URL` | Public URL (OAuth redirects + MCP clients) |

Install commands above generate the two secrets. Platform OAuth apps are configured in the UI, never in env: **Admin → OAuth Apps** holds the install-wide app every project uses (paste client ID/secret → Connect lights up on `/connect`), and a project owner can bring the project's own app instead under **Project settings → OAuth apps** — its own approval, branding and API limits.

---

## Connecting an AI client (MCP)

Endpoint: **`/mcp`**. Full guide: **[docs/tutorials/connect-ai-mcp.md](docs/tutorials/connect-ai-mcp.md)**.
Optional: install **[Fluxito Skills](fluxito-skills/)** so agents use tools the intended way.

### Hosted / public URL

1. Add a custom MCP server in Claude, ChatGPT, Cursor, etc.
2. URL: `https://your-host/mcp`
3. Complete OAuth (or use a PAT for headless clients — see the tutorial)

### Local via ngrok

```bash
ngrok http 8000
# set APP_BASE_URL to the https://….ngrok-free.app URL, restart app
# add https://….ngrok-free.app/mcp as the MCP connector
```

Update vendor OAuth redirect URIs when the public hostname changes. Free ngrok subdomains rotate on restart — keep the tunnel up while testing, or use a stable domain.

---

## Self-hosting

| Path | When |
|---|---|
| **Docker Compose** | Default — local, VPS, single team |
| **[render.yaml](render.yaml)** | Render one-click style |
| **[railway.json](railway.json)** | Railway + add Postgres/Redis |
| **Your infra** | Any Docker host; Postgres 15 + Redis 7 |

Default stack: `nginx` (port 8000) → `app` (FastAPI/Gunicorn) + `db` + `redis` (+ optional `updater` sidecar).

**MCP reverse proxy must not buffer** `/mcp` (chunked streaming):

```nginx
location /mcp {
    proxy_pass http://app:8001;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    add_header X-Accel-Buffering no always;
}
```

**Scaling:** default 4 Gunicorn workers (~`2 × vCPU + 1`). Plan ≥1 GB RAM for four workers.

**Secrets:** rotating `TOKEN_ENCRYPTION_KEY` without re-encrypting DB credentials permanently breaks stored connections.

---

## Backups

Postgres is the source of truth:

```bash
docker compose exec db pg_dump -U postgres fluxito > fluxito-$(date +%F).sql
```

Also back up `.env` (especially `TOKEN_ENCRYPTION_KEY`). Managed Postgres snapshots + a secrets manager are recommended in production.

---

## Common issues

| Symptom | Likely fix |
|---|---|
| MCP 401 | Re-authorize connector; check `APP_SECRET_KEY` / session expiry |
| `redirect_uri_mismatch` | `APP_BASE_URL` + path must match vendor console exactly |
| "OAuth app not configured" | Save client ID/secret under **Admin → OAuth Apps**, or the project's own under **Project settings → OAuth apps** |
| Disconnected after deploy | `TOKEN_ENCRYPTION_KEY` changed without re-encryption |
| Circuit breaker open | Fix root cause; check `/api/health`; wait ~60s |
| Google token revoked | Re-connect; Testing-mode apps expire refresh tokens quickly |
| Port conflict on compose | Host already using 5432/6379 — stop them or remap ports |

```bash
curl http://localhost:8000/api/health | python -m json.tool
docker compose logs -f app
docker compose exec app alembic upgrade head
docker compose exec app alembic current
```

---

## Maintenance

This community edition is a stable snapshot. It receives **security fixes only** — new features ship on [Fluxito Cloud](https://fluxito.app). Issues and PRs for bugs and security problems are welcome.

---

## Security

Report vulnerabilities privately via [GitHub Security Advisories](https://github.com/digitalXperiments/fluxito-community/security/advisories/new). See [SECURITY.md](SECURITY.md).

---

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Open an issue before large work so scope stays aligned.

---

## License

[Apache License 2.0](LICENSE). Copyright © 2026 Fluxito contributors. See [NOTICE](NOTICE) for third-party attributions.

Brand and platform names (Google Analytics, Meta, TikTok, Snowflake, Adobe, etc.) are trademarks of their respective owners. Fluxito is not affiliated with or endorsed by those vendors.
