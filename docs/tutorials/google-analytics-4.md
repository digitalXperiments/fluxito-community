# Google Analytics 4 setup

If you've completed [google-cloud-setup.md](google-cloud-setup.md), the OAuth app is ready. This guide covers what's specific to GA4: confirming the right APIs are enabled and making sure your Google account has access to the properties you want to query.

**Time:** ~10 minutes
**You'll need:** the shared Google OAuth app from [google-cloud-setup.md](google-cloud-setup.md), and at least Viewer access on the GA4 properties you want to query.

---

## 1. Confirm both GA4 APIs are enabled

In your GCP project, go to **APIs & Services → Enabled APIs & services** and verify both appear:

- **Google Analytics Data API** — used for running reports and querying events
- **Google Analytics Admin API** — used for listing properties and GA4 write operations

If either is missing, go to **APIs & Services → Library**, search by name, and click **Enable**.

---

## 2. Check your GA4 property access

Fluxito queries GA4 on behalf of the connected user, so that user needs access on the properties they want to use.

**Minimum roles:**
- **Viewer** — enough for all `analytics_read` (reports, events, real-time data)
- **Editor** — required for write operations (creating audiences, custom dimensions)

**To grant access:**
1. In GA4, click the gear icon to open **Admin**.
2. Under the **Property** column, click **Property Access Management**.
3. Click **+**, enter the user's email, choose their role, and click **Add**.

---

## 3. Connect your Google account

1. Go to `/connect` in Fluxito.
2. Click **Connect Google** and choose your tier:
   - **read-only** — for GA4 reports and event data
   - **full** — if you also need to create audiences or manage custom dimensions
3. Complete the Google OAuth flow, selecting the account that has GA4 property access.

---

## 4. Find your GA4 property ID

Several tools require a **property ID** (a plain integer like `123456789` — not the `G-XXXXXXXX` measurement ID).

Find it in GA4 under **Admin → Property Settings**, where it's labelled "PROPERTY ID."

---

## Troubleshooting

| Error | Fix |
|---|---|
| No properties returned | The connected Google account has no Viewer access on any GA4 property. Grant access in **GA4 Admin → Property Access Management**. |
| `PERMISSION_DENIED` on a report | The account's GA4 role is too low for the operation. Check and upgrade the role in Property Access Management. |
| `RESOURCE_EXHAUSTED` | GA4 Data API quota was exceeded. Wait for the daily reset (midnight Pacific time). |
| `insufficient_scope` on a write tool | The user connected with `readonly` scope. Reconnect at `/connect` and choose `full`. |
