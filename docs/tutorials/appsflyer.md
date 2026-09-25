# AppsFlyer setup

AppsFlyer is a credential connector — no OAuth app required. Each user connects by pasting their V2.0 API Token into Fluxito.

**Time:** ~5 minutes
**You'll need:** An AppsFlyer account with a V2.0 API Token.

---

## 1. Get your credentials

In your AppsFlyer dashboard, go to **Account Settings → V2.0 API Tokens**. You'll find:

- **V2.0 API Token** — a unique token that authenticates API requests to your account.

Copy your token.

---

## 2. Connect to Fluxito

1. Go to `/connect/appsflyer` in Fluxito.
2. Fill in a **Display Name** (e.g. "AppsFlyer Production").
3. Paste your **V2.0 API Token**.
4. Click **Connect**.

---

## 3. Available actions

Once connected, Fluxito can perform the following actions:

- `list_apps` — view all apps linked to your AppsFlyer account
- `get_installs_report` — retrieve installs report data with date range filtering
- `get_in_app_events_report` — retrieve in-app events report data
- `get_partners_report` — retrieve partner network report data

---

## 4. Disconnect

To disconnect your AppsFlyer account, go to the connect page in Fluxito and remove the connection. Alternatively, send a `DELETE /api/connections/appsflyer/{id}` request.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `Unauthorized` / `Invalid token` | Re-issue your V2.0 API Token from AppsFlyer **Account Settings**. The token may have expired. |
| Rate limit | AppsFlyer enforces per-app rate limits on data export endpoints. Spread out large requests. |

For more details, refer to the [AppsFlyer Developer Hub](https://dev.appsflyer.com/).
