# Adjust setup

Adjust is a credential connector — no OAuth app required. Each user connects by pasting their Adjust API Token into Fluxito.

**Time:** ~5 minutes
**You'll need:** An Adjust account with API token access.

---

## 1. Get your credentials

In your Adjust dashboard, go to your **user profile → Settings → API Token**. You'll find:

- **Adjust API Token** — your personal API token for authenticating requests to the Reports Service and Campaign APIs.

Copy your token.

---

## 2. Connect to Fluxito

1. Go to `/connect/adjust` in Fluxito.
2. Fill in a **Display Name** (e.g. "Adjust Production").
3. Paste your **Adjust API Token**.
4. Click **Connect**.

---

## 3. Available actions

Once connected, Fluxito can perform the following actions:

- `list_apps` — list apps via the Reports Service filters data endpoint
- `get_report` — retrieve a standard JSON report (dimensions, metrics, date period)
- `get_pivot_report` — retrieve a pivot report with custom dimensions and breakdown index
- `list_events` — list tracked events with their tokens and mappings
- `get_partner_links` — fetch partner trackers (links) for a specific app token via the Campaign API

---

## 4. Disconnect

To disconnect your Adjust account, go to the connect page in Fluxito and remove the connection. Alternatively, send a `DELETE /api/connections/adjust/{id}` request.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `Unauthorized` / `Invalid token` | Go to your Adjust user profile to re-issue the API Token. |
| 429 — rate limit | Adjust enforces 50 requests/second per source IP. Use exponential backoff. |

For more details, refer to the [Adjust Report Service API documentation](https://dev.adjust.com/en/api/rs-api/).
