# Meta Ads setup

Create a Meta Business App, add the Marketing API, and save the credentials in Fluxito so your team can connect their Meta Ads accounts.

**Time:** ~20 minutes
**You'll need:** a personal Facebook account, a Meta Business Manager account ([business.facebook.com](https://business.facebook.com/)), and admin access to your Fluxito install.

---

## 1. Open Meta for Developers

Go to [https://developers.facebook.com/](https://developers.facebook.com/) and sign in.

---

## 2. Create a Business app

1. Click **My Apps → Create App**.
2. Select **Business** as the app type and click **Next**.
3. Fill in the **App display name** (e.g. `Fluxito`), your **contact email**, and select your **Business Manager account** from the dropdown.
4. Click **Create App**.

---

## 3. Add the Marketing API product

On the App Dashboard, scroll to **Add Products to Your App**, find **Marketing API**, and click **Set up**. Click through any confirmation steps to add the product.

---

## 4. Add Facebook Login and set the redirect URI

1. In **Add Products to Your App**, find **Facebook Login** and click **Set up**. Choose **Web**.
2. Navigate to **Facebook Login → Settings** in the left sidebar.
3. Under **Valid OAuth Redirect URIs**, add:
   ```
   <APP_BASE_URL>/api/connections/meta/callback
   ```
   Replace `<APP_BASE_URL>` with your Fluxito URL (e.g. `https://fluxito.example.com`).
4. Ensure **Client OAuth Login** and **Web OAuth Login** are both **On**.
5. Click **Save Changes**.

---

## 5. Add the required permissions

Go to **App Review → Permissions and Features** and add:

- `ads_read` — read campaign performance data
- `ads_management` — create and modify ads (needed for write tools)
- `business_management` — access Business Manager hierarchy

In **development mode**, these work immediately for app admins and test users without needing App Review.

---

## 6. Get the App ID and App Secret

1. Go to **Settings → Basic**.
2. Copy the **App ID** (shown directly).
3. Click **Show** next to **App Secret** to reveal it, then copy it.

---

## 7. Save in Fluxito

1. In Fluxito, go to **Admin → OAuth Apps** and click **Configure** on the **Meta Ads** card.
2. Paste the **App ID** and **App Secret**.
3. Click **Test**, then **Save**.

---

## 8. Connect a Meta Ads account

Go to `/connect` in Fluxito and click **Connect Meta Ads**. Users are redirected to Meta's OAuth screen, where they select which ad accounts to share. On success, Fluxito stores a long-lived token (~60 days). Meta tokens don't auto-refresh — users must reconnect before the token expires.

---

## Production note

In development mode, only app admins and test users can connect. To allow other businesses to connect, switch to **Live mode** and complete App Review. `ads_read`, `ads_management`, and `business_management` all require Standard Access through App Review. App Review typically takes 3–10 business days; check Meta's current requirements before submitting. For a self-hosted instance connecting only your own or your agency's accounts, development mode may be sufficient.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `redirect_uri_mismatch` | The URI in **Facebook Login → Settings → Valid OAuth Redirect URIs** must exactly match `<APP_BASE_URL>/api/connections/meta/callback` — including `http` vs `https`. |
| `invalid_client` | Re-copy the App Secret from **Settings → Basic** (click Show) and update it in **Admin → OAuth Apps**. |
| App in development mode, user can't authorise | Add the user's account under **Roles → Test Users** in the App Dashboard. |
