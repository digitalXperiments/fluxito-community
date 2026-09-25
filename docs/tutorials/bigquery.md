# BigQuery setup

BigQuery supports two connection modes: a **service account** (recommended for production) or the **connected Google OAuth account** (simpler for personal use). Pick the one that fits your setup.

**Time:** ~15 minutes
**You'll need:** the shared Google OAuth app from [google-cloud-setup.md](google-cloud-setup.md), and IAM access to the GCP project where your BigQuery data lives.

---

## Mode A — Service account (recommended)

Use this when you want Fluxito to connect to BigQuery independently of any individual's Google account, or when the data lives in a GCP project your Google account doesn't have IAM access to.

### 1. Confirm the BigQuery API is enabled

In your GCP project, go to **APIs & Services → Enabled APIs & services** and verify **BigQuery API** is listed. If not, go to **Library** and enable it.

### 2. Create a service account

In the GCP project **where your BigQuery data lives**:

1. Go to **IAM & Admin → Service Accounts**.
2. Click **Create Service Account**. Name it `fluxito-bigquery`.
3. On the permissions step, add these three roles:
   - `BigQuery Data Viewer`
   - `BigQuery Job User`
   - `BigQuery Metadata Viewer`
4. Click **Continue**, then **Done**.

### 3. Generate a JSON key

1. In the Service Accounts list, click your new account's email to open its details.
2. Click the **Keys** tab → **Add Key → Create new key**.
3. Choose **JSON** and click **Create**. A `.json` file downloads automatically.

Keep this file secure — it grants read access to your BigQuery data.

### 4. Save in Fluxito

1. Go to `/connect` in Fluxito.
2. Click **Connect BigQuery**.
3. Paste the **entire contents** of the JSON key file into the credential field.
4. Optionally set a default project ID and dataset.
5. Click **Save**.

BigQuery credentials are per-user, per-connection. There's no BigQuery card in `/settings/integrations` — each Fluxito user goes to `/connect/bigquery` to save their own key.

---

## Mode B — Google OAuth (user's own account)

Use this if your Google account already has `BigQuery Data Viewer` + `BigQuery Job User` IAM roles in the GCP project where the data lives.

No extra steps needed — when you connect Google at `/connect` (any permission tier), BigQuery access is included automatically as long as the `bigquery` scope was added to the OAuth consent screen in [google-cloud-setup.md](google-cloud-setup.md).

---

## Troubleshooting

| Error | Fix |
|---|---|
| `User does not have bigquery.jobs.create permission` | The service account is missing `BigQuery Job User`. Add it in IAM at the project level. |
| `Access Denied: Table/Dataset` | The service account has job permission but no data access. Add `BigQuery Data Viewer` at the project or dataset level. |
| `Invalid JSON key / invalid_grant` | The pasted JSON is malformed or belongs to a deleted service account. Go to GCP Keys, delete the old key, create a new one, and paste the fresh JSON. |
| Dataset not found | Use `warehouse_read list_datasets` to see what's accessible. For cross-project access, grant the service account access in the other project's BigQuery UI. |
