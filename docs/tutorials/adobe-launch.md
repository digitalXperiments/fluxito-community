# Adobe Launch (Experience Platform Tags) setup

Add the Experience Platform Launch API to your Adobe I/O project — either reusing the same project you created for Adobe Analytics, or creating a new one — then grant the service account access to Launch properties.

**Time:** ~15 minutes (~5 minutes if reusing the Adobe Analytics project)
**You'll need:** an Adobe ID with **System Administrator** access in the Adobe Admin Console. If you completed [adobe-analytics.md](adobe-analytics.md), your project and credentials are ready to reuse.

---

## Choosing your path

### Path A — Reuse the Adobe Analytics project (recommended)

If you completed [adobe-analytics.md](adobe-analytics.md), you already have an Adobe I/O project with OAuth Server-to-Server credentials. The same Client ID, Client Secret, and Organization ID work for Launch too. Skip to Step 2.

### Path B — Create a new project

If you haven't set up Adobe Analytics yet, follow Steps 1–4 in [adobe-analytics.md](adobe-analytics.md), selecting **Experience Platform Launch API** instead of Adobe Analytics in Step 3. Then continue below from Step 2.

---

## 1. Add the Launch API to your existing project (Path A)

1. Open [https://developer.adobe.com/console/](https://developer.adobe.com/console/) and go to your existing project.
2. Click **Add API**.
3. Search for and select **Experience Platform Launch API** (may also be listed as **Adobe Experience Platform Tags**).
4. Click **Next**, then choose **Select existing credential** and select your existing OAuth Server-to-Server credential.
5. Click **Save configured API**.

---

## 2. Grant the service account access to Launch properties

Even with the API added, the service account needs to be added to a Launch product profile in the Adobe Admin Console.

1. Go to [https://adminconsole.adobe.com/](https://adminconsole.adobe.com/) and sign in as a System Administrator.
2. Click **Products**, then find **Adobe Experience Platform Tags** or **Adobe Experience Platform Launch**.
3. Click a product profile that covers the Launch properties you want Fluxito to access. Check the **Permissions** tab to see which properties are included and what rights are granted.
4. Click the **Developers** tab, then **Add developer** (or **+**).
5. Enter the **Technical Account email** from your Developer Console credential page (ends in `@techacct.adobe.com`). Click **Save**.

**Rights needed by Fluxito:**
- **View** or **Develop** — for `tagmanager_read` (inspecting rules, data elements, extensions)
- **Develop** + **Approve** or **Publish** — for `tagmanager_write` (creating rules, building libraries)

---

## 3. Collect credentials

Use the same three values from [adobe-analytics.md](adobe-analytics.md) Step 4 — **Client ID**, **Client Secret**, and **Organization ID**. They're identical.

---

## 4. Save in Fluxito

1. Go to `/connect` in Fluxito.
2. Click **Connect Adobe Launch** (or **Connect Adobe Experience Platform Tags**).
3. Paste the **Client ID**, **Client Secret**, and **Organization ID**.
4. Click **Save**.

If you reused the Analytics project, you'll enter the same three values you already saved for Adobe Analytics. Fluxito stores each connector's credentials separately — entering them twice is expected.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `401 Unauthorized` | Verify the three credential fields match the Developer Console values. |
| `403 Forbidden` / `Insufficient permissions` | The service account hasn't been added to a Launch product profile. Complete Step 2. |
| No properties returned | The product profile has no properties assigned, or the permissions don't include View or Develop rights. Check the profile's Permissions tab. |
| Credentials work for Analytics but not Launch | The Technical Account email was added to an Analytics profile but not a Launch profile. Complete Step 2 for a Launch-specific profile. |
