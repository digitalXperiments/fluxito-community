# Bing Webmaster Tools

Use this guide to connect Bing Webmaster Tools to Fluxito for search query stats, crawl stats, index coverage, and link counts across your verified sites.

Bing Webmaster Tools is an SEO / webmaster platform (not paid media). It uses Microsoft account / Entra ID OAuth 2.0 rather than a marketing ads API.

## Prerequisites

- A [Bing Webmaster Tools](https://www.bing.com/webmasters) account with at least one verified site.
- A Microsoft Entra ID (Azure AD) app registration with a client ID and client secret. See step 1.
- A personal Microsoft account that has access to the verified sites you want Fluxito to query. Fluxito uses the Microsoft `consumers` tenant, which targets personal Microsoft accounts (the most common case for Bing Webmaster).

## 1. Create the Microsoft app registration

1. Go to the [Azure portal](https://portal.azure.com) and open **Microsoft Entra ID -> App registrations -> New registration**.
2. Give the app a name (for example, `Fluxito Bing Webmaster`).
3. Under **Supported account types**, choose **Personal Microsoft accounts only** (consumers). This matches Fluxito's use of the Microsoft `consumers` tenant.
4. Under **Redirect URI**, select **Web** and enter:

   ```text
   https://YOUR-FLUXITO-DOMAIN/auth/bing/callback
   ```

5. Click **Register**.
6. Copy the **Application (client) ID** from the overview page.
7. Open **Certificates & secrets -> New client secret**, create a secret, and copy its **Value** immediately (it is shown only once).

### API permissions / scope

Fluxito requests the following scope during the OAuth flow:

```text
https://ssl.bing.com/webmaster/api.svc/json/ offline_access
```

- `https://ssl.bing.com/webmaster/api.svc/json/` grants access to the Bing Webmaster Tools API on behalf of the user.
- `offline_access` returns a refresh token so Fluxito stays connected without re-logging in.

You do not need to pre-add these as static API permissions in the app registration; Fluxito requests them at consent time. Make sure the app registration allows consumer (personal Microsoft account) sign-in so the consent screen appears.

## 2. Configure the OAuth app in Fluxito

Open:

```text
Settings -> Integrations -> Bing
```

Enter the Application (client) ID as the client ID and the client secret value as the client secret.

Confirm the callback URL configured in the Azure app registration exactly matches:

```text
https://YOUR-FLUXITO-DOMAIN/auth/bing/callback
```

## 3. Connect Bing Webmaster Tools

Open:

```text
Connections -> Bing Webmaster Tools
```

Or navigate directly to `/connect/bing`. Fluxito starts the Microsoft OAuth flow on the `consumers` tenant. Sign in with the personal Microsoft account that has access to your verified Bing Webmaster sites and approve the requested permissions.

After consent, Fluxito stores the access and refresh tokens and redirects you back to the home page with a confirmation.

## 4. Verify access

Ask your AI client:

```text
List my verified Bing Webmaster sites.
```

The AI uses the `seo_read` tool with the `bing_list_sites` action.

## Supported actions

All Bing Webmaster actions run through the `seo_read` tool.

| Tool | Action | Purpose |
| --- | --- | --- |
| `seo_read` | `bing_list_sites` | List the verified sites available to the connected Microsoft account. |
| `seo_read` | `bing_get_query_stats` | Read search query performance (impressions, clicks) for a site. |
| `seo_read` | `bing_get_crawl_stats` | Read Bingbot crawl activity for a site. |
| `seo_read` | `bing_get_index_coverage` | Read index coverage and indexing status for a site. |
| `seo_read` | `bing_get_link_counts` | Read inbound link counts for a site. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Connect button disabled | The Bing client ID and secret are saved under `Settings -> Integrations`. |
| OAuth redirect mismatch | The Azure redirect URI exactly matches `https://YOUR-FLUXITO-DOMAIN/auth/bing/callback`. |
| Consent fails or account is rejected | The app registration allows personal Microsoft accounts (consumers tenant). |
| Token exchange failed | The client secret value (not the secret ID) is configured, and it has not expired. |
| No sites returned | The connected Microsoft account has verified sites in Bing Webmaster Tools. |
| Token expired or revoked | Reconnect the account under `/connect/bing`. |
