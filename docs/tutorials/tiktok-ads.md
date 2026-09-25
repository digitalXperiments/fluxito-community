# TikTok Ads setup

Register a TikTok for Business app, configure permissions and the redirect URI, then save the credentials in Fluxito.

**Time:** ~20 minutes
**You'll need:** a TikTok for Business account ([business.tiktok.com](https://business.tiktok.com/)) and admin access to your Fluxito install.

---

## 1. Open the TikTok Business API portal

Go to [https://business-api.tiktok.com/portal/](https://business-api.tiktok.com/portal/) and sign in with your TikTok for Business account. If this is your first time, complete the developer registration form with your company details.

---

## 2. Create an app

1. In the portal, go to **My Apps** or **App Management** and click **Create App**.
2. Fill in:
   - **App name:** e.g. `Fluxito`
   - **App category:** choose the option for marketing or advertising API access
   - **App description:** brief description (e.g. "Analytics tool for TikTok Ads performance data")
   - **App icon:** upload a square logo (required)
   - **Company name / website**
3. Agree to the TikTok Business API Terms of Service and click **Create**.

---

## 3. Configure permissions

In your app's settings, find the **Permissions** or **Scopes** section and enable:

- `Ads Management` / `ad.read` — read campaigns, ad groups, and ads
- `Reporting` / `report.read` — access performance metrics
- `Ad Account Management` — list and access ad accounts
- `Campaign Management` — read campaign structure

For write operations (pausing campaigns, updating budgets), also enable:
- `Ad Management (Write)` / `ad.write`
- `Campaign Management (Write)`

---

## 4. Register the redirect URI

In the app's **OAuth Settings** or **Redirect URLs** section, add:

```
<APP_BASE_URL>/api/connections/tiktok/callback
```

Replace `<APP_BASE_URL>` with your Fluxito URL (e.g. `https://fluxito.example.com`). Save the configuration.

---

## 5. Get the App ID and App Secret

In the app's **Basic Information** or **Credentials** section, find:

- **App ID** — also called "Client Key"
- **App Secret** — click Show or Reveal to display it

Copy both and store them securely.

---

## 6. Save in Fluxito

1. In Fluxito, go to **Admin → OAuth Apps** and click **Configure** on the **TikTok Ads** card.
2. Paste the **App ID** and **App Secret**.
3. Click **Test**, then **Save**.

---

## 7. Connect a TikTok Ads account

Go to `/connect` in Fluxito and click **Connect TikTok Ads**. Users are redirected to TikTok's OAuth screen to authorise access to their Ads Manager accounts.

TikTok access tokens are short-lived (~24 hours), but Fluxito refreshes them automatically using the refresh token. Refresh tokens last ~30 days — if one expires, the user must reconnect.

---

## Production note

In sandbox/development mode, you can only access your own TikTok Ads accounts. To access other advertisers' accounts, submit the app for TikTok's production review — typically 1–2 weeks. For self-hosted Fluxito connecting only your own accounts, sandbox mode is usually sufficient.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `redirect_uri_mismatch` / `40003` | The redirect URI registered in the portal must exactly match `<APP_BASE_URL>/api/connections/tiktok/callback`. |
| `40001` / `Invalid app_id` | Re-copy the App ID and App Secret from the app's Basic Information page and update them in **Admin → OAuth Apps**. |
| Permissions not granted | The user didn't approve all permissions during OAuth. Ask them to reconnect at `/connect` and approve everything. |
