# Connect an AI Client with MCP

Fluxito is an MCP server. After you connect an AI client, the AI can call Fluxito tools against the project you authorize: read analytics, run audits, and use your KPI Library and Business Context.

Use this guide after the Fluxito server is running and you have created your first project.

## What you need

- A signed-in Fluxito account.
- At least one Fluxito project.
- A public URL for your Fluxito server.
- An MCP-compatible AI client, such as Claude, ChatGPT, Cursor, Windsurf, or another client that supports remote MCP servers.

For local testing, `localhost` usually is not reachable by the AI client. Use ngrok or a real domain.

## 1. Confirm the MCP URL

Your MCP endpoint is:

```text
https://your-fluxito-domain.example.com/mcp
```

For local Docker Compose through ngrok, it looks like:

```text
https://abc123.ngrok-free.app/mcp
```

The domain must match `APP_BASE_URL`. If `APP_BASE_URL` still points to `http://localhost:8000`, OAuth redirects from external AI clients will fail.

## 2. Add Fluxito inside your AI client

Open your AI client's connector, integrations, or MCP server settings.

Add a custom MCP server with:

| Field | Value |
|---|---|
| Name | Fluxito |
| URL | `https://your-fluxito-domain.example.com/mcp` |
| Auth | OAuth / browser authorization, if the client asks |

When the client opens Fluxito in a browser, sign in and approve access.

## 3. Pick the active project

Fluxito tools are project-scoped. If you have more than one project, ask your AI:

```text
Show my Fluxito projects and set the active project to <project name>.
```

After the project is active, the AI can use only that project's connections, audits, KPI Library, Business Context, and activity log.

## 4. First prompts to verify the connection

Try these in order:

```text
List my connected Fluxito platforms.
```

```text
Read my business context and KPI library, then summarize what you know about this project.
```

```text
List the Fluxito tools you can use and group them by analytics, tracking, audits, and knowledge.
```

If you have GA4 connected:

```text
Show the GA4 properties available in this project.
```

## 5. Local testing with ngrok

Start Fluxito first:

```bash
docker compose up -d
```

Start ngrok in a second terminal:

```bash
ngrok http 8000
```

Copy the HTTPS forwarding URL, then update `.env`:

```text
APP_BASE_URL=https://abc123.ngrok-free.app
```

Restart the app:

```bash
docker compose restart app
```

Use `https://abc123.ngrok-free.app/mcp` in your AI client.

## Connecting from remote or headless servers (SSH, containers, CI — no browser)

When the MCP client runs on a machine without a UI (or where you cannot open a browser and receive localhost redirects), the normal OAuth flow cannot complete.

### Recommended: Personal Access Tokens (PATs)

1. On any machine with a browser, sign in to Fluxito and go to **/profile**.
2. In the **MCP Access Tokens** card, give the token a name (e.g. `prod-box`) and choose an expiry.
3. Click **Create token**. The plaintext token is shown **once** together with a ready-to-paste config snippet.
4. Copy the `Authorization: Bearer fxt_pat_...` header (or the whole snippet).
5. On the remote machine, add the header to your MCP client's configuration for the Fluxito server (most HTTP-based MCP clients support custom headers or `env`/`headers` in the server definition).
6. The remote client can now call tools with no further browser interaction.

PATs are user-wide (they carry your identity and project memberships exactly like a normal OAuth session). They can be revoked instantly from the same Profile page.

### Alternative: Out-of-band (manual) OAuth code paste

Some clients (or small helper scripts you run on the remote) can print an authorization URL, let you complete the normal sign-in/consent flow in a local browser, and then accept a short-lived `code` that you paste back. When the client uses a special `redirect_uri` such as `urn:ietf:wg:oauth:2.0:oob` (or `oob`), Fluxito will display the code on a dedicated page instead of trying to redirect to an unreachable address. After pasting the code the client finishes the normal PKCE token exchange.

PATs are still preferred for most headless cases because they require no client-side OAuth state management across machines.

## Common issues

| Symptom | Fix |
|---|---|
| AI client cannot connect | Confirm the URL ends in `/mcp` and is publicly reachable. |
| OAuth redirects to localhost | Update `APP_BASE_URL` and restart the app. |
| Token gets 401 | Re-authorize the connector in the AI client. |
| AI cannot see data | Connect a platform under `/connect` and make sure the active project is correct. |
| Streaming stalls behind a proxy | Disable proxy buffering on `/mcp`. |

## What to do next

- **[Add the Fluxito Skill](/tutorials/fluxito-skill)** so your AI operates the MCP the right way — the connector gives it the tools, the Skill gives it the method.
- Add Business Context so answers use your business rules.
- Add KPI definitions so the AI uses your formulas.
- Connect Google first if you want GA4, GTM, Google Ads, Search Console, or BigQuery.
- Ask the AI to run your first audit.
