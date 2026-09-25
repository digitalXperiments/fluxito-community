# LinkedIn Ads setup

Create a LinkedIn app, request Marketing Developer Platform access (required for ad data), set the redirect URI, and save the credentials in Fluxito.

**Time:** ~20 minutes of setup; Marketing Developer Platform approval takes 1–5 business days.
**You'll need:** a LinkedIn account, a LinkedIn Company Page you admin, and admin access to your Fluxito install.

---

## 1. Open the LinkedIn Developer Portal

Go to [https://www.linkedin.com/developers/](https://www.linkedin.com/developers/) and sign in.

---

## 2. Create a LinkedIn app

1. Click **Create app**.
2. Fill in:
   - **App name:** e.g. `Fluxito`
   - **LinkedIn Page:** search for and select your company page
   - **Privacy policy URL:** required (use your actual URL)
   - **App logo:** upload a square image (required)
3. Check the legal agreement box and click **Create app**.

---

## 3. Verify the company page association

In your app's **Settings** tab, find the **Company Page** section and click **Verify**. LinkedIn sends a verification code to the page's admins — enter it to complete verification.

---

## 4. Request Marketing Developer Platform access

LinkedIn gates ad API access behind a product called **Marketing Developer Platform**.

1. In your app's **Products** tab, find **Marketing Developer Platform** and click **Request access**.
2. Fill in the use case form — be specific (e.g. "Self-hosted analytics tool that reads LinkedIn Ads performance data for our own ad accounts"). Vague answers are rejected.
3. Submit the request and wait for email confirmation. Reviews typically take 1–5 business days.

While waiting, the app can only access ad data for accounts where you are a Campaign Manager.

---

## 5. Add the redirect URI

1. In the **Auth** tab, find **Authorized redirect URLs for your app**.
2. Click **Add redirect URL** and enter:
   ```
   <APP_BASE_URL>/api/connections/linkedin/callback
   ```
   Replace `<APP_BASE_URL>` with your Fluxito URL. Click **Update**.

---

## 6. Get the Client ID and Client Secret

In the **Auth** tab:
- **Client ID** is shown directly. Copy it.
- Click the eye icon next to **Primary Client Secret** to reveal it. Copy it.

---

## 7. Save in Fluxito

1. In Fluxito, go to **Admin → OAuth Apps** and click **Configure** on the **LinkedIn Ads** card.
2. Paste the **Client ID** and **Primary Client Secret**.
3. Click **Test**, then **Save**.

---

## 8. Connect a LinkedIn Ads account

Once Marketing Developer Platform is approved, go to `/connect` in Fluxito and click **Connect LinkedIn Ads**. LinkedIn access tokens are valid for **60 days** with no auto-refresh — set a reminder to reconnect before the token expires.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `redirect_uri_mismatch` | The URL in the Auth tab must exactly match `<APP_BASE_URL>/api/connections/linkedin/callback`. |
| Marketing Developer Platform not approved | Check the Products tab for approval status. Ad scopes won't appear in the OAuth flow until approved. |
| `MEMBER_AUTHORIZATION_ERROR` | The connecting user doesn't have Campaign Manager role on the ad account. Grant it in LinkedIn Campaign Manager. |
| Token expired | LinkedIn tokens last ~60 days with no auto-refresh. User must reconnect at `/connect`. |
