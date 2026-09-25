# Search Console setup

If you've completed [google-cloud-setup.md](google-cloud-setup.md), the OAuth app is ready. This guide covers what's specific to Search Console: confirming the API is enabled and making sure your Google account has the right access on the properties you want to query.

**Time:** ~10 minutes
**You'll need:** the shared Google OAuth app from [google-cloud-setup.md](google-cloud-setup.md), and Full User or Owner access on the Search Console properties you want to query.

---

## 1. Confirm the Search Console API is enabled

In your GCP project, go to **APIs & Services → Enabled APIs & services** and verify **Google Search Console API** appears.

If it's missing, go to **APIs & Services → Library**, search for `Google Search Console API`, and click **Enable**.

---

## 2. Check your Search Console property access

Fluxito queries on behalf of the connected user, so that user needs adequate access on the properties.

| Access level | What Fluxito can do |
|---|---|
| **Full User** | Read search analytics, list sitemaps, inspect URLs |
| **Owner** | All Full User operations + submit and delete sitemaps |

**Restricted User** access is not sufficient — most API endpoints won't work.

**To grant access:**
1. Go to [https://search.google.com/search-console/](https://search.google.com/search-console/) as a verified Owner.
2. Click **Settings → Users and permissions**.
3. Click **Add user**, enter the email address, choose **Full**, and click **Add**.

---

## 3. Connect your Google account

1. Go to `/connect` in Fluxito.
2. Click **Connect Google** and choose your tier:
   - **read-only** — for search analytics and URL inspection
   - **full** — if you also need to submit or delete sitemaps
3. Complete the Google OAuth flow with the account that has Search Console access.

---

## 4. Property identifier format

When using Search Console tools, you specify the property by its site URL — the exact format matters:

- **Domain properties:** `sc-domain:example.com`
- **URL-prefix properties:** `https://www.example.com/` (with trailing slash)

Ask Claude to run `seo_read list_sites` to see the exact identifier strings for properties the connected account can access.

---

## Data freshness note

Search Console data is delayed. Query data with an end date at least **3 days in the past** — yesterday's data is usually not yet available.

---

## Troubleshooting

| Error | Fix |
|---|---|
| No sites returned | The connected account has no verified Search Console properties. Add it as a Full User on an existing property, or verify a new property. |
| `403 Forbidden` / insufficient permissions | The account has Restricted User access. Change the permission to Full in **Settings → Users and permissions**. |
| Empty impression/click data | You're querying dates within the 2–3 day lag window. Use dates at least 3 days in the past. |
| `insufficient scope` on sitemap write | The user connected with `readonly` scope. Reconnect at `/connect` with `full` tier. |
