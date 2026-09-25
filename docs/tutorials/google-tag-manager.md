# Google Tag Manager setup

If you've completed [google-cloud-setup.md](google-cloud-setup.md), the OAuth app is ready. This guide covers what's specific to GTM: confirming the Tag Manager API is enabled and making sure your Google account has the right access level on the containers you want to manage.

**Time:** ~10 minutes
**You'll need:** the shared Google OAuth app from [google-cloud-setup.md](google-cloud-setup.md), and at least Read access on the GTM containers you want to inspect or edit.

---

## 1. Confirm the Tag Manager API is enabled

In your GCP project, go to **APIs & Services → Enabled APIs & services** and verify that **Tag Manager API** appears in the list.

If it's missing, go to **APIs & Services → Library**, search for `Tag Manager API`, and click **Enable**.

---

## 2. Check your GTM access

GTM has its own access control — separate from Google Cloud. A user with full GCP admin rights still can't read a GTM container without GTM access.

**GTM roles and what Fluxito can do with each:**

| GTM role | Fluxito capabilities |
|---|---|
| **Read** | List containers, tags, triggers, variables, workspaces, and versions |
| **Edit** | All Read operations + create, update, and delete tags, triggers, and variables |
| **Publish** | All Edit operations + publish container versions to live |
| **Admin** | Full account administration |

For read-only work: **Read** is enough. For creating tags: **Edit**. For publishing: **Publish**.

**To grant GTM access:**
1. Sign in to [https://tagmanager.google.com/](https://tagmanager.google.com/) with an account that has Admin access.
2. Click the three-dot menu next to the account name and select **User Management**.
3. Click **+** to add a user, enter their email, choose their role, and save.

---

## 3. Connect your Google account

1. Go to `/connect` in Fluxito.
2. Click **Connect Google** and choose your tier:
   - **read-only** — for inspecting containers and running audits
   - **gtm_write** or **full** — to create tags, edit triggers/variables, and publish
3. Complete the Google OAuth flow with the account that has GTM access.

Both the **Fluxito scope tier** and the **GTM role** must allow the operation. For example: connecting with `full` scope but having only Read in GTM means tag creation will fail with `PERMISSION_DENIED`.

---

## 4. Working with workspaces

GTM changes happen inside **workspaces** — development environments within a container. The **Default Workspace** (ID: `1`) is always present. Write tools require you to specify a workspace ID.

To list available workspaces and their IDs, ask Claude to run `tagmanager_read list_workspaces` with your container ID.

---

## Troubleshooting

| Error | Fix |
|---|---|
| No containers returned | The connected account has no GTM access. Add it in GTM **Admin → User Management** with at least Read access. |
| `PERMISSION_DENIED` on tag creation | The account has Read but not Edit access in GTM, or the Fluxito scope is `readonly`. Upgrade the GTM role to Edit AND reconnect with `gtm_write` or `full`. |
| `PERMISSION_DENIED` on publish | The account has Edit but not Publish access. Upgrade to Publish in GTM User Management. |
| `insufficient scope` | Reconnect at `/connect` with `gtm_write` or `full` tier. |
