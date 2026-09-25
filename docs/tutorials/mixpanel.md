# Mixpanel setup

Mixpanel is a credential connector — no OAuth app required. Each user connects their own Mixpanel project by pasting the project's API Secret and Service Token directly into Fluxito.

**Time:** ~5 minutes
**You'll need:** access to a Mixpanel project. Admin access may be required to view the Service Token.

---

## 1. Select the correct project

If your Mixpanel org has multiple projects, make sure you're in the right one before copying credentials. The project selector is in the top-left of the Mixpanel interface.

Each project has its own independent API Secret and Service Token — credentials from one project won't work for another.

---

## 2. Open Project Settings

With the correct project selected, click your project name → **Project Settings**. Find the **API credentials** area.

---

## 3. Copy the API Secret and Service Token

- **API Secret** — found in Project Settings → Project Details. Used for the Query API.
- **Service Token** — found in the same area. Used for the Ingest API.

Copy both values.

---

## 4. Save in Fluxito

1. Go to `/connect` in Fluxito.
2. Click **Connect Mixpanel**.
3. Paste the **API Secret** and **Service Token**.
4. Add a display name (e.g. "Production").
5. Click **Save**.

---

## 5. Multiple projects

To connect multiple Mixpanel projects, repeat steps 1–4 for each project. Give each connection a distinct name so you can tell them apart.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `401 Unauthorized` | Re-copy both keys from Mixpanel Project Settings. Both must be from the same project. |
| No events returned | Verify the Mixpanel project has received events and the queried date range contains data. |
| Wrong project data | Delete the connection and re-add using keys from the correct project. |
