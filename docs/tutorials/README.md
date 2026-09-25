# Fluxito Tutorials

Step-by-step guides for using Fluxito end to end: projects, MCP connection, Business Context, KPI Library, audits, the Activity Log, and platform setup.

Written for marketing analysts, growth leads, and analytics operators who want to connect their stack and let an MCP-compatible AI operate Fluxito safely.

**Recommended reading order for new users:**
1. Start with [Projects and connections](projects-and-connections.md).
2. Connect your AI with [Connect an AI Client with MCP](connect-ai-mcp.md).
3. Use [Platform Setup Guides](platform-setup-guides.md) for each connector you need.
4. Add [Business Context](business-context.md) and [KPI Library](kpi-library.md).
5. Run [audits](audits-and-activity.md) and review what your AI did in the Activity Log.

Prefer to chat inside Fluxito instead of an external AI client? Add your own OpenAI-compatible Base URL, model, and API key under **Settings → AI providers** (presets for OpenAI and OpenRouter), then open **Chat**.

---

## Start here

| Tutorial | What it covers | Time |
|---|---|---|
| [projects-and-connections.md](projects-and-connections.md) | Create projects, configure platform apps, connect accounts, and keep active project scope clear. | ~8 min |
| [connect-ai-mcp.md](connect-ai-mcp.md) | Add Fluxito to Claude, ChatGPT, Cursor, Windsurf, or any MCP-compatible AI client. | ~10 min |
| [fluxito-skill.md](fluxito-skill.md) | Install the Fluxito Skill so your AI follows the correct method and guardrails. | ~5 min |
| [platform-setup-guides.md](platform-setup-guides.md) | Hub for Google, paid media, warehouse, analytics, and tag-manager connector setup. | ~10 min |
| [business-context.md](business-context.md) | Write the Markdown context document the AI uses to understand your business rules. | ~10 min |

---

## How to use Fluxito features

| Tutorial | What it covers | Time |
|---|---|---|
| [kpi-library.md](kpi-library.md) | Define metrics, formulas, aliases, owners, targets, and executable KPI inputs. | ~15 min |
| [audits-and-activity.md](audits-and-activity.md) | Run platform audits, use vendor rule books and test flows, and review every AI tool call in the Activity Log. | ~12 min |

---

## Platform setup guides

Use these after you know which data sources a project needs.

## Google platforms — shared OAuth app

All five Google-backed connectors share a single OAuth 2.0 client created in Google Cloud. Complete the foundational guide first; the platform-specific tutorials only cover what differs.

| Tutorial | What it covers | Time |
|---|---|---|
| [google-cloud-setup.md](google-cloud-setup.md) | **Start here.** Create a GCP project, enable 6 APIs, configure the OAuth consent screen, and create the OAuth client ID. | ~20 min |
| [google-analytics-4.md](google-analytics-4.md) | Confirm GA4 APIs are enabled; check property access (Viewer/Editor roles); find your property ID. | ~10 min |
| [google-tag-manager.md](google-tag-manager.md) | Confirm Tag Manager API is enabled; check GTM container roles (Read/Edit/Publish). | ~10 min |
| [google-ads.md](google-ads.md) | Apply for a Google Ads developer token (required separately); Basic vs Standard access in one line. | ~15 min + up to 1–3 day approval |
| [search-console.md](search-console.md) | Confirm Search Console API is enabled; check Full User / Owner access; property identifier formats. | ~10 min |
| [bigquery.md](bigquery.md) | Two connection modes: service account JSON (recommended) and Google OAuth. Service account IAM setup. | ~15 min |

---

## Social and paid marketing — OAuth apps

Each platform requires its own OAuth app. App Review timelines are noted for platforms that require production review.

An OAuth app can be set up once for the whole install (**Admin → OAuth Apps**, super admins) or per project (**Project settings → OAuth apps**, project owners and admins). A project's own app takes precedence for that project; every other project keeps the install's. Register the same redirect URIs either way. Switching a project to a different app flags its existing connections for that platform to be reconnected.

| Tutorial | What it covers | Time |
|---|---|---|
| [meta-ads.md](meta-ads.md) | Business App creation, Marketing API product, Facebook Login redirect URI, App Review note. | ~20 min setup; 3–10 day review |
| [tiktok-ads.md](tiktok-ads.md) | Business API app creation, permissions, redirect URI, production review note. | ~20 min setup; 1–2 week review |
| [linkedin-ads.md](linkedin-ads.md) | App creation, company page verification, Marketing Developer Platform request, redirect URI. | ~20 min setup; 1–5 day review |
| [pinterest-ads.md](pinterest-ads.md) | Developer portal app registration, scope configuration, redirect URI, production review note. | ~15 min setup; 1–5 day review |
| [x-ads.md](x-ads.md) | X developer app setup, OAuth 1.0a callback, Ads API access, account permission checks. | ~20 min setup; access review varies |
| [snap-ads.md](snap-ads.md) | App creation, Ads API capability, confidential client setup, redirect URI. | ~20 min setup; variable review |
| [reddit-ads.md](reddit-ads.md) | Reddit Ads API app, OAuth credentials, account access verification. | ~15 min setup |
| [bing-webmaster.md](bing-webmaster.md) | Bing Webmaster Tools API key, site verification, usage limits. | ~10 min |

---

## Data warehouses — credential connectors

No OAuth app required. Each user connects with their own database credentials via `/connect`.

| Tutorial | What it covers | Time |
|---|---|---|
| [snowflake.md](snowflake.md) | Account identifier format, dedicated user/role SQL, warehouse and database grants, future table grants. | ~15 min |
| [redshift.md](redshift.md) | Cluster endpoint discovery, dedicated user creation, VPC security group inbound rule. | ~20 min |

---

## Analytics and tag management — credential connectors

No OAuth app required.

| Tutorial | What it covers | Time |
|---|---|---|
| [adjust.md](adjust.md) | API token authentication; reports, events and partner links. | ~5 min |
| [amplitude.md](amplitude.md) | API Key and Secret Key per project; multi-project setup. | ~5 min |
| [appsflyer.md](appsflyer.md) | V2.0 API Token authentication; installs, events and partner reports. | ~5 min |
| [branch.md](branch.md) | Branch Key + Branch Secret authentication; app config and daily exports. | ~5 min |
| [braze.md](braze.md) | Braze API Key + REST endpoint; campaigns, canvases, segments, and user tracking. | ~5 min |
| [moengage.md](moengage.md) | App ID + REST API Key + Data Center; users, campaigns, events, and messaging. | ~5 min |
| [mixpanel.md](mixpanel.md) | API Secret and Service Token per project; multi-project setup. | ~5 min |
| [posthog.md](posthog.md) | Personal API Key, host URL, and Project ID; Cloud or self-hosted. | ~5 min |
| [adobe-analytics.md](adobe-analytics.md) | Adobe Developer Console project, OAuth Server-to-Server credential, Admin Console product profile grant. | ~25 min |
| [adobe-launch.md](adobe-launch.md) | Reuse or create Adobe I/O project for Experience Platform Tags; product profile rights. | ~15 min (~5 min if reusing Analytics project) |
| [adobe-marketo.md](adobe-marketo.md) | Marketo LaunchPoint custom service (API-only user + role), Client ID/Secret, REST endpoint. Own credentials — not Adobe IMS. | ~15 min |

---

## Cross-references

- **AI clients use MCP:** complete [connect-ai-mcp.md](connect-ai-mcp.md), then use platform setup guides to give the AI real data access.
- **Business Context + KPI Library come before analysis:** complete [business-context.md](business-context.md) and [kpi-library.md](kpi-library.md) before asking your AI for executive summaries or KPI analysis.
- **Rule books drive tracking audits:** enable vendor rule books under Audit → Vendors & rules (see [audits-and-activity.md](audits-and-activity.md)) before asking for GA4/GTM implementation drift checks.
- **Google connectors share one OAuth client:** once you complete [google-cloud-setup.md](google-cloud-setup.md) and connect a Google account, that single connection covers GA4, GTM, Google Ads, Search Console, and BigQuery (OAuth mode).
- **Adobe Analytics and Adobe Launch share one Adobe I/O project:** complete [adobe-analytics.md](adobe-analytics.md) first, then [adobe-launch.md](adobe-launch.md) reuses the same three credentials with one additional Admin Console step.
- **Adobe Marketo Engage is separate from the other Adobe connectors:** [adobe-marketo.md](adobe-marketo.md) uses Marketo's own LaunchPoint credentials and REST endpoint, not the Adobe I/O project — there are no shared credentials with Adobe Analytics/Launch.
- **BigQuery service account vs Google OAuth:** BigQuery is the only Google connector with an alternative credential mode. See [bigquery.md](bigquery.md) for both options.
