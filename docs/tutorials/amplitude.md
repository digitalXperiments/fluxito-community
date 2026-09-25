# Amplitude setup

Amplitude is a credential connector — no OAuth app or install-level setup required. Each user connects their own Amplitude project by pasting the project's API Key and Secret Key directly into Fluxito.

**Time:** ~5 minutes
**You'll need:** access to an Amplitude project. Admin access may be required to view the Secret Key depending on your organisation's settings.

---

## 1. Select the correct project

If your Amplitude org has multiple projects, make sure you're in the right one before copying credentials. The project selector is in the top-left of the Amplitude interface.

Each project has its own independent API Key and Secret Key — credentials from one project won't work for another.

---

## 2. Open Project Settings

With the correct project selected, click **Settings** in the left sidebar (or the gear icon), then look for a **General** or **Project settings** section. Find the **API credentials** area.

---

## 3. Copy the API Key and Secret Key

- **API Key** — a 32-character string. Shown directly.
- **Secret Key** — click **Show** or **Reveal** if it's hidden. If you can't see it, ask an Amplitude admin in your organisation.

Copy both values.

---

## 4. Save in Fluxito

1. Go to `/connect` in Fluxito.
2. Click **Connect Amplitude**.
3. Paste the **API Key** and **Secret Key**.
4. Add a **Project name** if prompted (e.g. "Production App") to identify this connection.
5. Click **Save**.

---

## 5. Multiple projects

To connect multiple Amplitude projects, repeat steps 1–4 for each project. Give each connection a distinct name so you can tell them apart.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `401 Unauthorized` / `Invalid credentials` | Re-copy the API Key and Secret Key from Amplitude **Settings → API credentials** for the specific project. Both keys must be from the same project. |
| No events returned | Verify the Amplitude project has received events and the queried date range contains data. |
| Wrong project data returned | Delete the connection at `/connect` and re-add using keys from the correct project. |
