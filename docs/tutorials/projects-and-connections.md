# Use Projects and Connections

Projects are isolated workspaces. Each project has its own platform connections, audits, KPI Library, Business Context, and activity log.

Use projects to separate clients, brands, business units, or environments.

## 1. Create a project

After signing in, go to:

```text
Projects
```

Create a project with a clear name, such as:

```text
Acme Ecommerce - Production
```

Avoid vague names like `Test` or `Main` if multiple people will use the installation.

## 2. Set the active project

Fluxito routes most UI pages and MCP tool calls through the active project.

If your AI seems to be reading the wrong account or cannot find a connection, ask it:

```text
List my Fluxito projects and set the active project to <project name>.
```

## 3. Configure platform OAuth apps

Install admins configure platform app credentials under:

```text
Settings -> Integrations
```

This is where you paste OAuth Client IDs and Client Secrets for Google, Meta, TikTok, LinkedIn, Pinterest, Snap, and other app-based connectors.

You usually do this once per Fluxito installation.

## 4. Connect accounts to a project

Project users connect real platform accounts under:

```text
/connect
```

Examples:

- Connect a Google account that has GA4, GTM, Google Ads, Search Console, or BigQuery access.
- Connect Meta Ads for ad account reporting.
- Connect a warehouse with service credentials.
- Connect Amplitude or Adobe credentials.

## 5. Confirm connection coverage

Ask your AI:

```text
List the connected platforms for this active project and tell me what each one can be used for.
```

Or check the home page connection summary.

## 6. Recommended first setup path

For most teams:

1. Create a project.
2. Configure Google OAuth in Settings -> Integrations.
3. Connect Google under `/connect`.
4. Add Business Context.
5. Add 3-5 core KPIs.
6. Connect an AI client with MCP.
7. Run your first audit.

## Common issues

| Issue | Fix |
|---|---|
| Connect button is disabled | Configure that platform under Settings -> Integrations first. |
| OAuth redirect mismatch | Ensure vendor redirect URI matches `APP_BASE_URL` plus the callback path. |
| AI cannot see a newly connected platform | Confirm the active project and reconnect if the token expired. |
| Wrong GA4/GTM account appears | Sign in with the Google account that has access to the desired property/container. |
| Multiple projects get mixed up | Use explicit project names in prompts and set the active project first. |

## Project hygiene

- Use one project per client or production analytics environment.
- Keep test and production projects separate.
- Keep Business Context and KPI Library project-specific.
- Review Activity Log when multiple AI clients or users operate in the same project.
