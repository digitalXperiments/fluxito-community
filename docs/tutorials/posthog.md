# PostHog setup

PostHog is a credential connector — no OAuth app required. Each user connects their own PostHog project by pasting a Personal API Key, the project's host URL, and the Project ID. PostHog can be self-hosted, so the host URL is per-connection.

**Time:** ~5 minutes
**You'll need:** access to a PostHog project (Cloud or self-hosted).

---

## 1. Find your Project Host

This is the URL you use to access PostHog:

- **PostHog Cloud (US)**: `https://app.posthog.com`
- **PostHog Cloud (EU)**: `https://eu.posthog.com`
- **Self-hosted**: your instance URL (e.g. `https://posthog.yourcompany.com`)

---

## 2. Find your Project ID

In PostHog, go to **Project Settings** (gear icon → Project). The **Project ID** is a number shown at the top of the settings page.

---

## 3. Create a Personal API Key

In **Project Settings**, find **Personal API Keys** (or go to your user profile → Personal API Keys). Click **Create personal API key**. Give it a name (e.g. "Fluxito"). Copy the key — it starts with `phx_` and is only shown once.

---

## 4. Save in Fluxito

1. Go to `/connect` in Fluxito.
2. Click **Connect PostHog**.
3. Enter the **Display name** (e.g. "Production").
4. Enter the **Project Host** (the URL from step 1, e.g. `https://app.posthog.com`).
5. Enter the **Project ID** (the number from step 2).
6. Enter the **API Key** (the key from step 3).
7. Click **Save**.

---

## 5. Multiple projects / instances

To connect multiple PostHog projects, repeat steps 1–4 for each project. Self-hosted instances each need their own connection with the correct host URL.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `401 Unauthorized` | Re-copy the Personal API Key. Verify it hasn't been revoked in PostHog. |
| `404 Not Found` | Check the Project ID and Project Host are correct. A wrong host for a self-hosted instance is the most common cause. |
| Connection works but no data | Verify the PostHog project has received events and the queried date range contains data. |
| Self-hosted: can't connect | Ensure your Fluxito instance can reach your PostHog host (network/firewall). |
