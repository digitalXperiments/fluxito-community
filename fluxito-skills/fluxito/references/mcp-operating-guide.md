# Fluxito MCP — operating guide

How to operate **any** Fluxito tool. Feature methodology (audits) lives
in its own reference; this is the shared mechanics.

## 1. Connect + pick a project

- The user connects the **Fluxito MCP** connector in their AI client and must have an
  **active project**.
- `get_session_context` (no args) returns: the active project, connected vs disconnected
  platforms (with connect URLs), and the tool list. **Call it first** in a new session.
- One project? It's auto-selected. Otherwise `list_my_projects` → `set_active_project`.
  Call `set_active_project` in its **own turn** before scoped calls (or pass `project_id`
  in the same-turn call).
- Auth / no-active-project errors → stop and tell the user to connect + select a project.

## 2. The tool surface (what to reach for)

| Domain | Read | Write | Audit |
|---|---|---|---|
| Product/web analytics (GA4, Amplitude, Adobe) | `analytics_read` | `analytics_write` | `run_audit` (`ga4_*`, `amplitude_*`, `adobe_*`) |
| Tag manager (GTM, Adobe Launch) | `tagmanager_read` | `tagmanager_write` | `run_audit` (`gtm_*`, `adobe_launch_*`) |
| Paid ads + Marketo | `marketing_read` | `marketing_write` | `run_audit` (`marketing_*`, `marketo_*`) |
| Warehouse (BigQuery/Redshift/Snowflake) | `warehouse_read` + `warehouse_query` (SQL) | — | `run_audit` (`warehouse_*`) |
| Organic search (GSC, Bing) | `seo_read` | `seo_write` | `run_audit` (`seo_*`) |
| Knowledge (KPIs, business context) | `get_knowledge` | — (edit in the web UI) | — |
| Cross-connector insight | `run_analysis` | — | — |
| Tag rule book / live tag test | `tag_rulebook`, `live_tag_test` | same | same |

Adobe Analysis Workspace lives on `analytics_read` / `analytics_write` as
**`adobe_workspace_*` actions** (the ADOBE WORKSPACE group in `describe`). Create with
`config.tables=[{metrics, dimension?}]` — do **not** invent raw Workspace JSON.

Cheap catalog reads live in the `*_read` tools; **heavier audits live in `run_audit`** so
reads stay fast. Audits return `findings[]` with severity + recommendation.

## 3. The self-describing contract (never guess params)

Every dispatcher takes `action` (a string enum — pick from the description's list) and
`params` (an object). To learn an action's params without trial and error:

- **`describe`** — `tool(action="describe")` lists every action's spec; `tool(action=
  "describe", params={"action":"run_report"})` returns one action's required/optional
  params, types, and an example. Use it whenever unsure.
- **Errors teach you** — omit a required param and the tool returns
  `{error:true, error_type:"missing_required_param", missing:[…], required:[…],
  optional:[…], example:{…}}`. Add what `missing` says and retry.
- **`error_type`** is a stable, documented set: `missing_required_param`, `invalid_param`,
  `unknown_action`, `unknown_tool`, `not_connected`, `insufficient_scope`,
  `not_implemented`, `upstream_error`, `server_error`. Branch on it, don't parse prose.
- **Platform validity** — an analytics/tag/marketing action is valid only for the
  platform(s) shown in `[brackets]` in the description. Always pass `platform` (or
  `engine` for warehouse). The wrong platform → `unknown_action`.

## 4. Scopes & reversibility

- Read scopes cover `*_read`, `run_audit`, `run_analysis`, `get_*`. Write scopes are
  required for `*_write`; `publish_container`
  needs a separate publish scope. An out-of-scope call returns
  `error_type:"insufficient_scope"`.
- Reversibility: `propose_change` (GTM) is a dry-run. Creates are usually reversible;
  `publish_container`, `delete_*`, and ad budget/status changes
  are **not** — confirm with the user first.

## 5. Batching with `run_script`

`run_script` runs several tool calls in one shot (parallel where independent). Inside a
script you call a dispatcher with **both** levels of nesting:
`call('run_audit', {'action': 'gtm_audit_container', 'params': {...}})`. Use it to fan a
report across properties or run an audit suite, then synthesize. Prefer individual calls
when you need to reason between steps.

## 6. Capability roles (why audits are platform-agnostic)

Findings reason about **roles**, not products: `tag_inventory` (is it configured?),
`event_volume` (is data flowing?), `conversion_config` (is it set for activation?).
GA4/GTM/Google Ads fill these today; Adobe/Amplitude/warehouse fill the same roles later
with no change to the method. `readiness.unfilled_roles` tells you which capabilities no
connected platform currently provides — say so honestly.
