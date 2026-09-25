# Use the KPI Library

The KPI Library is the project's source of truth for metric definitions. It tells the AI what each KPI means, what formula to use, which fields feed it, what direction is good, and who owns it.

Use it for metrics that appear in reporting, audits, weekly summaries, and executive analysis.

## What a KPI contains

Each KPI can store:

| Area | Examples |
|---|---|
| Identity | Name, slug, aliases, status, version |
| Definition | Description, business question, interpretation guide |
| Formula | Inputs from GA4, BigQuery, Snowflake, Redshift, or other supported sources |
| Quality | Unit, format, direction, expected range, target |
| Ownership | Owner and source-of-truth URL |

Approved KPIs with bound inputs can be computed by the AI through MCP.

## 1. Open the KPI Library

Go to:

```text
Knowledge -> KPI Library
```

Or open:

```text
/kpi-library
```

## 2. Add a KPI

Click **Add KPI** and fill the core fields first:

| Field | Recommended value |
|---|---|
| Name | Human-readable metric name, such as `Customer Acquisition Cost` |
| Slug | Stable lowercase ID, such as `cac` |
| Status | Use `Draft` while designing, `Approved` when ready |
| Description | Plain-language definition |
| Business question | The question this KPI answers |
| Interpretation guide | How to read good, bad, or suspicious values |

Add aliases for common stakeholder terms. For example, a KPI named `Customer Acquisition Cost` might have aliases `CAC`, `cost per customer`, and `acquisition cost`.

## 3. Bind formula inputs

In the Formula section, click **Add input**.

Pick:

1. Source, such as GA4 or BigQuery.
2. Connection.
3. Property, dataset, table, metric, or column.
4. Input key, such as `spend`, `customers`, `sessions`, or `revenue`.

Then write the expression using curly braces:

```text
{spend} / {customers}
```

More examples:

```text
{transactions} / {sessions}
```

```text
({revenue} - {ad_spend}) / {ad_spend}
```

## 4. Set quality metadata

These fields make the AI's answers better:

| Field | Why it matters |
|---|---|
| Unit | Lets the AI format the answer correctly: `%`, `USD`, `count`, `ratio`. |
| Direction | Tells whether higher or lower is better. |
| Target value | Lets reports compare actuals against goals. |
| Expected range | Helps flag suspicious or impossible results. |
| Time grain | Clarifies whether the KPI is daily, weekly, monthly, or rolling. |

## 5. Approve the KPI

Set status to **Approved** once the definition and formula are ready.

Draft KPIs can still document definitions, but the AI treats them as incomplete and should qualify any answer that uses them.

## 6. Ask the AI to use KPIs

Try:

```text
List our approved KPIs and tell me which ones are ready to compute.
```

```text
Compute CAC for the last 30 days using the KPI Library definition.
```

```text
Use our KPI Library to summarize last week's most important acquisition and revenue KPIs.
```

```text
Check whether any KPI values are outside their expected ranges.
```

## Good first KPIs

| Business type | KPI ideas |
|---|---|
| Ecommerce | Conversion rate, revenue, AOV, refund rate, ROAS, checkout completion |
| Lead generation | Lead conversion rate, qualified lead rate, CPL, SQL rate, demo booking rate |
| SaaS | Trial signup rate, activation rate, CAC, MRR, churn, expansion revenue |
| Media | Sessions, engaged sessions, ad revenue, RPM, newsletter signup rate |

## Common mistakes

| Mistake | Fix |
|---|---|
| Leaving status as draft forever | Approve stable KPIs so the AI can trust them. |
| Using platform-default definitions without checking | Add your internal definition in the description. |
| No owner | Add the team or person responsible for the metric. |
| Formula input names are vague | Use clear keys like `ad_spend`, `orders`, and `new_customers`. |
| No expected range | Add sanity bounds for important executive metrics. |

## How it works with MCP

The AI can:

- List KPIs.
- Read the full definition of a KPI.
- Compute an approved KPI against bound sources.
- Use KPI direction, unit, target, and interpretation guide in reporting.

The KPI Library does not replace raw platform data. It makes raw data reusable and business-specific.
