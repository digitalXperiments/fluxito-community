# Google Ads setup

If you've completed [google-cloud-setup.md](google-cloud-setup.md), the OAuth app is ready. Google Ads has one extra requirement: a **developer token** issued from your Google Ads manager account. You need both.

**Time:** ~15 minutes of setup; developer token approval (if applying for Standard access) takes 1–3 business days. Basic access is instant and sufficient for most use cases.
**You'll need:** the shared Google OAuth app from [google-cloud-setup.md](google-cloud-setup.md), and a Google Ads manager account (MCC).

---

## 1. Confirm the Google Ads API is enabled

In your GCP project, go to **APIs & Services → Enabled APIs & services** and verify **Google Ads API** appears.

If it's missing, go to **APIs & Services → Library**, search for `Google Ads API`, and click **Enable**.

---

## 2. Apply for a developer token

The Google Ads API requires a **developer token** in addition to your OAuth credentials. It's issued from your manager account (MCC), not from GCP.

1. Sign in to your Google Ads manager account at [https://ads.google.com/](https://ads.google.com/).
2. Click the wrench icon (**Tools & Settings**) → **Setup → API Center**. Or go directly to [https://ads.google.com/aw/apicenter](https://ads.google.com/aw/apicenter).
3. If you don't have a token yet, fill in the application form:
   - **Use case:** describe what you're building (e.g. "Self-hosted analytics tool that reads Google Ads performance data for accounts I manage").
   - **Access level:** start with **Basic** — it's approved instantly and works for your own accounts and accounts directly under your MCC.
4. Click **Apply**. Copy the developer token shown on the page.

**Basic vs Standard:** Basic access covers your own accounts and client accounts directly under your MCC — this is sufficient for most self-hosted Fluxito deployments. Apply for Standard only if you need to query accounts outside your MCC hierarchy.

---

## 3. Save the developer token in Fluxito

1. Sign in to Fluxito as an admin and go to **Admin → OAuth Apps**.
2. Click **Configure** on the **Google** card.
3. Paste the Client ID and Client Secret (from google-cloud-setup.md) if you haven't already.
4. In the **Google Ads Developer Token** field, paste the token from Step 2.
5. Click **Test**, then **Save**.

---

## 4. Connect your Google account

1. Go to `/connect` in Fluxito.
2. Click **Connect Google** and choose at least `readonly` tier (the `adwords` scope is included in all tiers).
3. Complete the Google OAuth flow with the account that has Google Ads access.

---

## 5. Find your customer ID

Google Ads customer IDs are 10-digit numbers displayed in the top bar of the Ads interface (formatted as `XXX-XXX-XXXX`). When passing them to Fluxito tools, use the format without dashes (`XXXXXXXXXX`).

---

## Troubleshooting

| Error | Fix |
|---|---|
| `DEVELOPER_TOKEN_PARAMETER_MISSING` | Go to **Admin → OAuth Apps → Google** (or the project's **Project settings → OAuth apps → Google**) and confirm the developer token field is filled in. |
| `DEVELOPER_TOKEN_NOT_APPROVED` | You're querying an account outside your MCC hierarchy with Basic access. Either query only accounts under your MCC, or apply for Standard access. |
| `CUSTOMER_NOT_FOUND` | Verify the customer ID (remove all dashes). Check the account is accessible from your manager account. |
| `USER_PERMISSION_DENIED` | The connected Google account doesn't have access to that ad account. Grant access inside Google Ads: **Tools & Settings → Account access → Users**. |
