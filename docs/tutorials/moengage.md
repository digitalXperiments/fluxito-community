# MoEngage setup

MoEngage is a credential connector — no OAuth app required. Each user connects by pasting their App ID, REST API Key, and Data Center code into Fluxito.

**Time:** ~5 minutes
**You'll need:** A MoEngage account with REST API access.

---

## 1. Get your credentials

In your MoEngage dashboard (dashboard.moengage.com), go to **Settings → APIs → Transaction APIs** (or **App settings**). You'll find:

- **App ID** — a unique identifier for your MoEngage app.
- **REST API Key** — a secret key that authenticates API requests to your account.

Also note your **Data Center** code. It is visible in the dashboard URL or under account settings. The data center determines the API host:

| Data Center | API Host |
|---|---|
| DC-01 | `https://api-01.moengage.com` |
| DC-02 | `https://api-02.moengage.com` |
| DC-03 | `https://api-03.moengage.com` |

Copy your App ID, REST API Key, and Data Center code.

---

## 2. Connect to Fluxito

1. Go to `/connect/moengage` in Fluxito.
2. Fill in a **Display Name** (e.g. "MoEngage Production").
3. Paste the **App ID**.
4. Paste the **REST API Key**.
5. Select or enter the **Data Center** code.
6. Click **Connect**.

---

## 3. Available actions

Once connected, Fluxito can perform the following actions:

**Read actions:**
- `get_user_info` — retrieve user profile details by ID or email
- `list_campaigns` — list all campaigns in your MoEngage workspace
- `get_campaign_details` — get detailed metrics and configuration for a specific campaign
- `list_events` — list custom events tracked in your MoEngage app

**Write actions:**
- `create_user` — create a new user profile
- `update_user` — update user attributes on an existing profile
- `send_push` — send a push notification to a user or audience segment
- `send_email` — send a transactional or campaign email
- `send_sms` — send an SMS message to a user or audience segment

---

## 4. Disconnect

To disconnect your MoEngage account, go to the connect page in Fluxito and remove the connection. Alternatively, send a `DELETE /api/connections/moengage/{id}` request.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `401 Unauthorized` / `Invalid API key` | Re-copy the REST API Key from MoEngage **Settings → APIs → Transaction APIs**. Verify the key has the required permissions. |
| `404 Not Found` / Wrong host | Your Data Center code determines the API host. Check your dashboard URL to confirm the correct data center and update the connection. |
| Rate limit exceeded | MoEngage enforces rate limits per API key. See [MoEngage API rate limits](https://docs.moengage.com/docs/rate-limiting) for details. |

For more details, refer to the [MoEngage documentation](https://docs.moengage.com/).
