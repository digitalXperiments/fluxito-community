# Pinterest Ads setup

Register a Pinterest app, configure scopes, add the redirect URI, and save the credentials in Fluxito.

**Time:** ~15 minutes
**You'll need:** a Pinterest Business account ([business.pinterest.com](https://business.pinterest.com/)) and admin access to your Fluxito install.

---

## 1. Open the Pinterest Developers portal

Go to [https://developers.pinterest.com/](https://developers.pinterest.com/) and sign in with your Pinterest account (must be a Business account or linked to one).

---

## 2. Create an app

1. Navigate to **My Apps** and click **Connect app** or **Create new app**.
2. Fill in:
   - **App name:** e.g. `Fluxito`
   - **Description:** brief description of the app
   - **App type:** choose **Web application** (server-side)
3. Agree to the Pinterest Developer Terms of Service and click **Create**.

---

## 3. Configure OAuth scopes

In the app's **Permissions** or **Scopes** section, select:

- `ads:read` — read campaigns, ad groups, ads, and performance reports (required)
- `user_accounts:read` — read basic account info (required)
- `ads:write` — create and modify campaigns (needed for write tools)

---

## 4. Register the redirect URI

In the app's **Redirect URIs** or **OAuth settings** section, add:

```
<APP_BASE_URL>/api/connections/pinterest/callback
```

Replace `<APP_BASE_URL>` with your Fluxito URL (e.g. `https://fluxito.example.com`). Save.

---

## 5. Get the App ID and App Secret

In the app's **App credentials** section, find:

- **App ID** (also called Client ID)
- **App secret key** (also called Client Secret) — click **Show** or **Reveal**

Copy both and store them securely.

---

## 6. Save in Fluxito

1. In Fluxito, go to **Admin → OAuth Apps** and click **Configure** on the **Pinterest Ads** card.
2. Paste the **App ID** and **App secret key**.
3. Click **Test**, then **Save**.

---

## 7. Connect a Pinterest Ads account

Go to `/connect` in Fluxito and click **Connect Pinterest Ads**. Users authorise access to their Pinterest Business account and ad accounts. Fluxito uses refresh tokens to extend the session, but if the refresh token expires (~30 days), the user must reconnect.

---

## Production note

Pinterest apps start in development mode — only your own account and invited testers can connect. For production access, submit an app review through the developer portal. Pinterest's review is generally faster than Meta or LinkedIn — typically 1–5 business days for read-only apps.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `redirect_uri_mismatch` | The registered URI must exactly match `<APP_BASE_URL>/api/connections/pinterest/callback` — including scheme and path. |
| `invalid_client` | Re-copy the App ID and App secret key from the app's credentials section and update them in **Admin → OAuth Apps**. |
| App in development mode, user can't authorise | Add the user as a tester in the developer portal, or submit for production review. |
