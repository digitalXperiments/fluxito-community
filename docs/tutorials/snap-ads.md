# Snapchat Ads setup

Register a Snap app, add the Ads API capability, configure the redirect URI, then save the credentials in Fluxito.

**Time:** ~20 minutes
**You'll need:** a Snap Business account ([business.snapchat.com](https://business.snapchat.com/)) and admin access to your Fluxito install.

---

## 1. Open the Snap for Developers portal

Go to [https://developers.snap.com/](https://developers.snap.com/) and sign in with your Snapchat account.

---

## 2. Create an app

1. Navigate to **My Apps** and click **Create App** (or **New App**).
2. Fill in:
   - **App name:** e.g. `Fluxito`
   - **Description:** brief description
   - **Organization / Business:** select your Snap Business account
   - **App icon:** upload a square image (required)
3. Agree to Snap's developer terms and submit.

---

## 3. Add the Ads API capability

In your app's dashboard, look for a **Capabilities** or **API Access** section. Find **Ads API** or **Marketing API** and click **Add** or **Enable**.

For development access (your own Snap Business accounts), this should be available immediately. For access to other advertisers' accounts, Snap requires a production review.

---

## 4. Configure as a confidential client

In the app's OAuth settings, ensure the app is configured as:

- **Client type:** Confidential (server-side)
- **Grant type:** Authorization Code

This ensures Snap uses the client secret in the token exchange, which is the correct setting for a server-side application like Fluxito.

---

## 5. Register the redirect URI

In the app's **OAuth settings** or **Redirect URIs** section, add:

```
<APP_BASE_URL>/api/connections/snap/callback
```

Replace `<APP_BASE_URL>` with your Fluxito URL. Save.

---

## 6. Get the Client ID and Client Secret

In the app's **Credentials** section:

- **Client ID** — copy it directly
- **Client Secret** — click **Show** or **Reveal**, then copy

---

## 7. Save in Fluxito

1. In Fluxito, go to **Admin → OAuth Apps** and click **Configure** on the **Snap Ads** card.
2. Paste the **Client ID** and **Client Secret**.
3. Click **Test**, then **Save**.

---

## 8. Connect a Snapchat Ads account

Go to `/connect` in Fluxito and click **Connect Snap Ads**. Users authorise access to their Snap Ads Manager account. Fluxito refreshes access tokens automatically, but if the refresh token expires, users must reconnect.

---

## Production note

In development mode, you can only access ad accounts under your own Snap Business organisation. To access other advertisers' accounts, submit a production access application through the developer portal. Approval timeline varies.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `redirect_uri_mismatch` | The registered URI must exactly match `<APP_BASE_URL>/api/connections/snap/callback` — check scheme and path. |
| `invalid_client` | Re-copy the Client ID and Client Secret from the app's credentials section and update them in **Admin → OAuth Apps**. |
| `Insufficient permissions` / `401` | Verify the Ads API capability is enabled on the app, and that the connecting user has ad account access in Snap Business Manager. |
