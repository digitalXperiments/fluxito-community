# Platform Setup Guides

Fluxito can operate only on platforms that are connected to the active project. Use this page as the hub for platform credential and OAuth setup.

For most teams, start with Google. One Google OAuth app unlocks GA4, Google Tag Manager, Google Ads, Search Console, and BigQuery OAuth mode.

## Recommended setup order

1. Create a project in Fluxito.
2. Configure the platform OAuth app under `Settings -> Integrations`.
3. Connect a real user or service account under `/connect`.
4. Confirm the AI can see the connected platform.
5. Add Business Context, KPIs, and audits on top.

## Google platforms

Complete [Google Cloud setup](google-cloud-setup.md) first.

| Guide | Use it for |
|---|---|
| [Google Cloud setup](google-cloud-setup.md) | Shared OAuth client for Google-backed connectors |
| [Google Analytics 4](google-analytics-4.md) | Properties, reports, events, custom definitions, conversions |
| [Google Tag Manager](google-tag-manager.md) | Containers, tags, triggers, variables, implementation audits |
| [Google Ads](google-ads.md) | Campaign performance, conversion diagnostics, budget and quality audits |
| [Search Console](search-console.md) | SEO clicks, impressions, queries, pages, sitemap and mover audits |
| [BigQuery](bigquery.md) | Warehouse queries through service account JSON or Google OAuth |

## Paid media platforms

Each paid platform needs its own OAuth app.

| Guide | Use it for |
|---|---|
| [Meta Ads](meta-ads.md) | Facebook and Instagram advertising data |
| [TikTok Ads](tiktok-ads.md) | TikTok campaign performance |
| [LinkedIn Ads](linkedin-ads.md) | LinkedIn campaign performance |
| [Pinterest Ads](pinterest-ads.md) | Pinterest advertising data |
| [X Ads](x-ads.md) | X campaign and line item performance |
| [Reddit Ads](reddit-ads.md) | Reddit campaign and ad group performance |
| [Apple Ads](apple-ads.md) | App Store campaign and ad group performance |
| [Snap Ads](snap-ads.md) | Snapchat marketing API |

## SEO and webmaster tools

These connectors read search and SEO performance, not paid media. [Search Console](search-console.md) uses the shared Google OAuth app (see Google platforms above); Bing Webmaster Tools uses its own Microsoft OAuth app.

| Guide | Use it for |
|---|---|
| [Search Console](search-console.md) | Google search clicks, impressions, queries, pages, sitemap and mover audits |
| [Bing Webmaster Tools](bing-webmaster.md) | Bing search query stats, crawl stats, index coverage, and link counts |

## Warehouses

Warehouse connectors usually use credentials, not a marketing OAuth app.

| Guide | Use it for |
|---|---|
| [Snowflake](snowflake.md) | Query Snowflake databases and audit warehouse health |
| [Redshift](redshift.md) | Query Redshift schemas and audit table health |
| [BigQuery](bigquery.md) | Query BigQuery datasets and tables |

## Analytics and tag management

| Guide | Use it for |
|---|---|
| [Adjust](adjust.md) | Reports Service reports, events, pivot and partner links |
| [Amplitude](amplitude.md) | Product analytics event queries |
| [AppsFlyer](appsflyer.md) | Installs, in-app events and partner reports |
| [Branch](branch.md) | App config and daily data exports |
| [Mixpanel](mixpanel.md) | Product analytics event query |
| [PostHog](posthog.md) | Product analytics (Cloud or self-hosted) |
| [Adobe Analytics](adobe-analytics.md) | Adobe report suites |
| [Adobe Launch](adobe-launch.md) | Adobe Experience Platform Tags properties |
| [Adobe Marketo Engage](adobe-marketo.md) | Marketo leads, campaigns & automation |

## Confirm the connection

After connecting a platform, ask your AI:

```text
List the connected platforms in this Fluxito project and show what actions each one supports.
```

For Google:

```text
List my available GA4 properties, GTM containers, Google Ads accounts, Search Console sites, and BigQuery projects.
```

## Troubleshooting checklist

| Issue | Check |
|---|---|
| Connect button disabled | Platform app credentials are saved under `Settings -> Integrations`. |
| OAuth redirect mismatch | Vendor redirect URI exactly matches the Fluxito callback URL. |
| AI cannot see platform | The platform is connected to the active project. |
| Token expired or revoked | Reconnect the account under `/connect`. |
| Missing account/property | The signed-in platform user has the required permissions. |
