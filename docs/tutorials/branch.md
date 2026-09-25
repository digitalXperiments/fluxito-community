# Branch setup

Branch is a credential connector — no OAuth app required. Each user connects by pasting their Branch Key and Branch Secret into Fluxito.

**Time:** ~5 minutes
**You'll need:** A Branch account with key and secret access.

---

## 1. Get your credentials

In your Branch dashboard, go to **Configuration → Security & Access → Credentials**. You'll find:

- **Branch Key** — a public app identifier. Shown directly.
- **Branch Secret** — a private secret that authenticates your requests. Copy it carefully.

Copy both values.

---

## 2. Connect to Fluxito

1. Go to `/connect/branch` in Fluxito.
2. Fill in a **Display Name** (e.g. "Branch Production").
3. Paste the **Branch Key** and **Branch Secret**.
4. Click **Connect**.

---

## 3. Available actions

Once connected, Fluxito can perform the following actions:

- `get_app` — fetch app configuration (deep-link settings, bundle ID, URI schemes, SDK config)
- `request_daily_export` — request a daily data export (returns S3 file paths for installs, opens, clicks, etc.)

Branch's [Daily Export API](https://help.branch.io/apidocs/daily-exports-api.md) provides attribution data for a 7-day rolling window. Exported files are hosted on S3 and can be downloaded directly.

---

## 4. Disconnect

To disconnect your Branch account, go to the connect page in Fluxito and remove the connection. Alternatively, send a `DELETE /api/connections/branch/{id}` request.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `Invalid credentials` | Re-copy the Branch Key and Secret from Branch **Configuration → Security & Access → Credentials**. |
| Rate limit exceeded | Branch's App API enforces rate limits per account. Wait before retrying. |

For more details, refer to the [Branch API documentation](https://help.branch.io/apidocs/apis-overview.md).
