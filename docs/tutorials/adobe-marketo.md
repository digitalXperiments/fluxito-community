# Adobe Marketo Engage setup

Create a Marketo **Custom Service** (a LaunchPoint API-only user) to get an OAuth Client ID and Secret, find your REST endpoint, and save all three in Fluxito.

> **Note:** Marketo Engage does **not** use Adobe IMS / the Adobe Developer Console like Adobe Analytics and Adobe Launch do. It has its own REST API and its own OAuth credentials, configured entirely inside your Marketo instance. An Adobe Analytics credential will not work here, and vice versa.

**Time:** ~15 minutes
**You'll need:** Marketo **Admin** access (to create an API-only user, a role, and a LaunchPoint service).

---

## 1. Create an API-only user role

1. In Marketo, go to **Admin → Users & Roles → Roles**.
2. Click **New Role**. Name it e.g. `Fluxito API`.
3. Grant the **Access API** permissions you need. For full Fluxito functionality (read + write), include **Read-Only** and **Read-Write** access to Lead, Activity, Campaign, and Asset endpoints.
4. Click **Create**.

---

## 2. Create an API-only user

1. Go to **Admin → Users & Roles → Users → Invite New User**.
2. Enter an email (a dedicated mailbox is fine), select the **Fluxito API** role.
3. On the next step, check **API Only** and uncheck the interactive-access options.
4. Click **Invite**.

---

## 3. Create a LaunchPoint Custom Service

1. Go to **Admin → Integration → LaunchPoint**.
2. Click **New → New Service**.
3. Set **Service** to **Custom**.
4. Give it a **Display Name** (e.g. `Fluxito`) and select the **API Only** user from Step 2.
5. Click **Create**.

---

## 4. Collect your credentials

1. Back on the **LaunchPoint** page, find your new service and click **View Details**.
2. Copy the **Client ID** and **Client Secret**.

---

## 5. Find your REST endpoint

1. Go to **Admin → Integration → Web Services**.
2. Under **REST API**, copy the **Endpoint** URL. It looks like:
   `https://123-ABC-456.mktorest.com/rest`
3. For Fluxito, use the base **without** the trailing `/rest` — i.e. `https://123-ABC-456.mktorest.com`. (Fluxito appends the API paths for you, and trailing slashes are trimmed automatically.)

| Field | Where to find it |
|---|---|
| **Client ID** | Admin → Integration → LaunchPoint → your service → View Details |
| **Client Secret** | Same panel — copy it immediately |
| **REST endpoint** | Admin → Integration → Web Services → REST API → Endpoint |

---

## 6. Save the credentials in Fluxito

1. In Fluxito, open **Connect → Adobe Marketo Engage** (`/connect/marketo`).
2. Enter a **Display name**, your **Marketo REST endpoint** (e.g. `https://123-ABC-456.mktorest.com`), **Client ID**, and **Client Secret**.
3. Click **Connect**.

Fluxito uses the OAuth client-credentials grant to obtain a short-lived access token and caches it per instance.

---

## What you can do once connected

Marketo actions are exposed through the unified `marketing_read` / `marketing_write` tools (prefix `marketo_`) and `run_audit`:

- **Leads & people:** `marketo_get_leads`, `marketo_get_lead`, `marketo_list_lists`, `marketo_get_list_leads`, `marketo_get_lead_activities` (opens, clicks, form fills, email engagement)
- **Campaigns & programs:** `marketo_list_campaigns`, `marketo_list_programs`, `marketo_get_program`
- **Email & asset inventory:** `marketo_list_emails`, `marketo_list_landing_pages`, `marketo_list_forms`
- **Writes:** `marketo_upsert_leads`, `marketo_add_to_list`, `marketo_remove_from_list`, `marketo_request_campaign`, `marketo_schedule_campaign`
- **Audit:** `marketo_audit_instance` (API usage vs. daily quota + program inventory), `marketo_check_data_quality`

> **Heads-up on API limits:** Marketo enforces a daily API call quota and a concurrency limit. Fluxito keeps calls minimal and surfaces a clear "rate limited / quota exceeded" message if you hit them.
