# Google Cloud setup

Connect your Google account to Fluxito so your AI can access GA4, Google Tag Manager, Google Ads, Search Console, and BigQuery — all through a single OAuth app you create here.

**Time:** ~20 minutes
**You'll need:** a Google account (any personal or Workspace account), and admin access to your Fluxito install.

---

## 1. Open Google Cloud Console

Go to [https://console.cloud.google.com/](https://console.cloud.google.com/) and sign in.

---

## 2. Create a project

1. Click the project selector in the top navigation bar (next to the Google Cloud logo).
2. Click **New project**.
3. Name it something like `fluxito-connectors`. Leave the other fields as defaults.
4. Click **Create** and wait for the project to become active.

---

## 3. Enable the six APIs

In the left sidebar, go to **APIs & Services → Library**. Search for each API below, click the result, and click **Enable**:

| API name to search for |
|---|
| `Google Analytics Data API` |
| `Google Analytics Admin API` |
| `Tag Manager API` |
| `Google Ads API` |
| `Google Search Console API` |
| `BigQuery API` |

Enable all six before moving on.

---

## 4. Configure the OAuth consent screen

Go to **APIs & Services → OAuth consent screen**.

1. Choose **External** as the User Type and click **Create**.
2. Fill in **App name** (e.g. `Fluxito`), **User support email**, and **Developer contact information**. Click **Save and Continue**.
3. On the **Scopes** step, click **Add or Remove Scopes** and add all of the following:
   - `https://www.googleapis.com/auth/analytics.readonly`
   - `https://www.googleapis.com/auth/analytics`
   - `https://www.googleapis.com/auth/tagmanager.readonly`
   - `https://www.googleapis.com/auth/tagmanager.edit.containers`
   - `https://www.googleapis.com/auth/tagmanager.publish`
   - `https://www.googleapis.com/auth/tagmanager.manage.accounts`
   - `https://www.googleapis.com/auth/adwords`
   - `https://www.googleapis.com/auth/webmasters.readonly`
   - `https://www.googleapis.com/auth/webmasters`
   - `https://www.googleapis.com/auth/bigquery`
   - `https://www.googleapis.com/auth/bigquery.readonly`

   Click **Update**, then **Save and Continue**.

4. On the **Test users** step, click **Add users** and add the Google account email addresses of everyone who will connect to Fluxito. (While the app is in Testing mode, only listed accounts can authorise.) Click **Save and Continue**, then **Back to Dashboard**.

---

## 5. Create the OAuth 2.0 client ID

1. Go to **APIs & Services → Credentials**.
2. Click **Create Credentials → OAuth client ID**.
3. Set **Application type** to **Web application**. Give it a name like `Fluxito Web`.
4. Under **Authorised redirect URIs**, add all three of these (replace `<APP_BASE_URL>` with your Fluxito URL, e.g. `https://fluxito.example.com` or `http://localhost:8000`):
   ```
   <APP_BASE_URL>/auth/google/data/callback
   <APP_BASE_URL>/auth/google/identity/callback
   <APP_BASE_URL>/auth/google/signin/callback
   ```
5. Under **Authorised JavaScript origins**, add your base URL (no path):
   ```
   <APP_BASE_URL>
   ```
6. Click **Create**.

A dialog shows your **Client ID** and **Client Secret**. Copy both immediately — the secret is only shown once here (you can retrieve it again from the credential's edit page).

---

## 6. Save the credentials in Fluxito

1. Sign in to Fluxito as an admin and go to **Admin → OAuth Apps**.
2. Click **Configure** on the **Google** card.
3. Paste the Client ID and Client Secret. Click **Test**, then **Save**.

---

## 7. Connect a Google account

Go to `/connect` in Fluxito and click **Connect Google**. Choose a permission tier:

- **Read-only** — GA4 reports, GTM read, Ads read, Search Console read, BigQuery read
- **GTM write** — all read-only plus create/edit/publish GTM containers
- **Full access** — everything, including GA4 write and Ads management

After authorising, all five Google connectors (GA4, GTM, Ads, Search Console, BigQuery OAuth) are active under that one connected account.

---

## Production note

The app is in **Testing** mode, which supports up to 100 users. For public deployments (any Google account can connect), you need to publish the app and submit for Google verification. Apps that request sensitive scopes (which these do) require verification before the consent screen shows without a warning. Verification takes 4–6 weeks. For a small internal team, Testing mode is sufficient indefinitely.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `redirect_uri_mismatch` | The URI in the error must exactly match one registered in **Credentials → Edit OAuth client** — check for `http` vs `https`, wrong port, or missing path segment. |
| `Access blocked: App is not verified` | The authorising account is not on the test users list. Add it under **OAuth consent screen → Test users**. |
| `API has not been enabled` | Go to **APIs & Services → Library** and enable the API named in the error. |
| `invalid_client` | Re-copy the Client Secret from **Credentials → Edit OAuth client** and update it in **Admin → OAuth Apps**. |
