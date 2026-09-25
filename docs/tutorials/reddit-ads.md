# Reddit Ads

Use this guide to connect Reddit Ads to Fluxito for campaign reporting, ad group reporting, tracking diagnostics, and campaign status and budget updates.

## Prerequisites

- A Reddit account with access to the ad accounts you want Fluxito to query.
- A Reddit OAuth app with the **web app** type created at [https://www.reddit.com/prefs/apps](https://www.reddit.com/prefs/apps).
- The OAuth app's client ID and client secret.

## 1. Create a Reddit OAuth app

1. Go to [https://www.reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) and sign in as the Reddit user that has access to your ad accounts.
2. Scroll to the bottom and click **create another app**.
3. Fill in the form:
   - **Name** — choose a name (e.g. `Fluxito`).
   - **Type** — select **web app**.
   - **Redirect URI** — enter your Fluxito callback URL (see below).
4. Click **create app**.
5. Note the **client ID** (shown below the app name) and the **secret**.

Use this callback URL in the Reddit app form:

```text
https://YOUR-FLUXITO-DOMAIN/auth/reddit/callback
```

## 2. Configure the OAuth app in Fluxito

Open:

```text
Settings -> Integrations -> Reddit
```

Enter the client ID and client secret from the Reddit app you created above.

## 3. Connect Reddit Ads

Open:

```text
Connections -> Reddit Ads
```

Fluxito starts the Reddit OAuth 2.0 flow. Sign in as the Reddit user that can access the target ad accounts and approve the requested scopes.

### Required OAuth scopes

| Scope | Purpose |
| --- | --- |
| `identity` | Identify the connected Reddit user. |
| `adsread` | Read campaign and ad group performance data. |
| `history` | Access post and activity history for tracking diagnostics. |

## 4. Verify access

Ask your AI client:

```text
List my connected Reddit Ads accounts.
```

The AI uses the `marketing_read` tool with `platform="reddit"` and `action="list_accounts"`.

## Supported actions

| Tool | Action | Purpose |
| --- | --- | --- |
| `marketing_read` | `list_accounts` | List ad accounts available to the connected Reddit user. |
| `marketing_read` | `get_campaign_performance` | Read campaign performance for a date range. |
| `marketing_read` | `get_adgroup_performance` | Read ad group performance for a date range. |
| `marketing_audit` | `audit_tracking_setup` | Check Reddit Pixel availability and event coverage. |
| `marketing_write` | `update_campaign_status` | Pause or activate a campaign. |
| `marketing_write` | `update_campaign_budget` | Update the daily or lifetime budget for a campaign. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| OAuth redirect mismatch | The redirect URI in the Reddit app exactly matches the Fluxito callback URL. |
| No accounts returned | The connected Reddit user has advertiser access in Reddit Ads Manager. |
| `adsread` scope denied | The Reddit app was created as **web app** type; script and installed app types do not support all scopes. |
| Token expired or revoked | Reconnect the account under `/connect`. |
| Analytics returns a permission error | The Reddit user has the required role on the target ad account. |
