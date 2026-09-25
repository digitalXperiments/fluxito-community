# Braze setup

Braze is a credential connector — no OAuth app required. Each user connects by pasting their Braze API Key and REST endpoint URL into Fluxito.

**Time:** ~5 minutes
**You'll need:** A Braze account with REST API access.

---

## 1. Get your credentials

In your Braze dashboard (dashboard.braze.com), go to **Settings → Console → API Keys → Create New API Key** (or **Company Settings → API Console**). You'll find:

- **API Key** — a unique key that authenticates API requests to your Braze account.
- **REST Endpoint URL** — your cluster-specific endpoint, shown on the same API Console page.

The REST endpoint depends on which Braze dashboard cluster your account is on:

| Cluster | REST Endpoint |
|---|---|
| US-01 | `https://rest.iad-01.braze.com` |
| EU-01 | `https://rest.fra-01.braze.eu` |
| SG-01 | `https://rest.sgp-01.braze.com` |

Copy both the API Key and the REST endpoint URL.

---

## 2. Connect to Fluxito

1. Go to `/connect/braze` in Fluxito.
2. Fill in a **Display Name** (e.g. "Braze Production").
3. Paste the **API Key**.
4. Paste the **REST Endpoint URL**.
5. Click **Connect**.

---

## 3. Available actions

Once connected, Fluxito can perform the following actions:

**Read actions:**
- `list_campaigns` — list all campaigns in your Braze workspace
- `get_campaign_details` — get detailed metrics and configuration for a specific campaign
- `list_canvases` — list all Canvases in your Braze workspace
- `get_canvas_details` — get detailed metrics and configuration for a specific Canvas
- `list_segments` — list all segments in your Braze workspace
- `get_segment_details` — get detailed metrics and configuration for a specific segment

**Write actions:**
- `track_users` — record custom events, purchases, and user attribute updates via the `/users/track` endpoint
- `send_message` — send immediate messages (email, push, SMS, webhook) to a targeted audience
- `trigger_campaign` — trigger a campaign to send to a specific user or audience segment
- `trigger_canvas` — trigger a Canvas flow for a specific user or audience segment
- `delete_users` — delete user profiles by external ID or email address

---

## 4. Disconnect

To disconnect your Braze account, go to the connect page in Fluxito and remove the connection. Alternatively, send a `DELETE /api/connections/braze/{id}` request.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `401 Unauthorized` / `Invalid API key` | Re-copy the API Key from Braze **Settings → API Console**. Verify the API Key has the required permissions enabled. |
| `404 Not Found` / Wrong endpoint | Your REST endpoint URL is cluster-specific. Check your Braze dashboard URL to confirm the correct cluster and update the endpoint accordingly. |
| Rate limit exceeded | Braze enforces rate limits per API key. See [Braze API rate limits](https://www.braze.com/docs/api/rate_limits/) for details. |

For more details, refer to the [Braze API overview](https://www.braze.com/docs/api/overview/).
