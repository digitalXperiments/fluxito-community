# X Ads

Use this guide to connect X Ads to Fluxito for campaign reporting, line item reporting, website tag checks, and campaign status updates.

## Prerequisites

- An X developer app with Ads API access.
- The app's API Key and API Key Secret.
- An X user that has access to the ad accounts you want Fluxito to query.

## 1. Configure the OAuth app in Fluxito

Open:

```text
Settings -> Integrations -> X
```

Enter the API Key as the client ID and the API Key Secret as the client secret.

Use this callback URL in the X developer portal:

```text
https://YOUR-FLUXITO-DOMAIN/auth/x/callback
```

## 2. Connect X Ads

Open:

```text
Connections -> X Ads
```

Fluxito starts the X OAuth 1.0a flow. Sign in as the X user that can access the target ad accounts and approve the app.

## 3. Verify access

Ask your AI client:

```text
List my connected X Ads accounts.
```

The AI uses the `marketing_read` tool with `platform="x"` and `action="list_accounts"`.

## Supported actions

| Tool | Action | Purpose |
| --- | --- | --- |
| `marketing_read` | `list_accounts` | List ad accounts available to the connected X user. |
| `marketing_read` | `get_campaign_performance` | Read campaign performance for a date range. |
| `marketing_read` | `get_line_item_performance` | Read line item performance for a date range. |
| `marketing_audit` | `audit_tracking_setup` | Check X website tag availability. |
| `marketing_write` | `update_campaign_status` | Pause or activate a campaign. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| OAuth request token fails | The X API Key and Secret are configured correctly in Settings. |
| No accounts returned | The connected X user has Ads account access in X Business. |
| Analytics returns an API error | The app has Ads API access and the account has the required permission level. |
