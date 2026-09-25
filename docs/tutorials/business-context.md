# Use Business Context

Business Context is a Markdown document that tells the AI how your business works. It covers details that raw data cannot explain: business model, audience, terminology, goals, seasonality, campaign rules, conversion definitions, and known caveats.

The AI can load this context through MCP before answering analytics questions.

## When to use it

Use Business Context when you want the AI to avoid generic answers and reason in your terms.

Good examples:

- Your company calls a qualified demo request an "SQL".
- Revenue should exclude refunds and test orders.
- Paid campaigns are judged on pipeline, not only purchases.
- Ramadan, back-to-school, Black Friday, or a product launch changes normal seasonality.
- A metric has an internal definition that differs from the platform default.

## 1. Open Business Context

In Fluxito, go to:

```text
Knowledge -> Business Context
```

Or open:

```text
/business-context
```

## 2. Fill the document

Start with this structure:

```markdown
# About the business

## Business model
We sell...

## Audience
- Primary:
- Secondary:

## North-star goals
- 

## Conversion definitions
- Lead:
- Qualified lead:
- Purchase:

## Important rules
- Refunds are excluded after...
- Internal traffic is...

## Seasonality
- 

## Current campaigns and launches
- 

## Known data caveats
- 
```

Keep it practical. The best context is short enough to stay readable and specific enough to change how the AI answers.

## 3. Save the document

Click **Save**. The document is stored for the active project.

If you use multiple projects, each project can have its own Business Context.

## 4. Ask the AI to use it

Use prompts like:

```text
Read our Business Context before answering. Then explain why paid conversion rate changed last week.
```

```text
Use our Business Context and KPI Library to write a weekly executive summary.
```

```text
Based on our business rules, are these GA4 conversions named correctly?
```

## 5. Keep it fresh

Update Business Context when something changes:

- A new product line launches.
- A conversion definition changes.
- A campaign, market, or region becomes important.
- A known data caveat is fixed.
- A stakeholder changes the way metrics should be interpreted.

## Common mistakes

| Mistake | Better approach |
|---|---|
| Writing a long company brochure | Write operational facts the AI should use in analysis. |
| Defining formulas here only | Put KPI formulas in the KPI Library and refer to them here. |
| Forgetting temporary context | Add launches, outages, promos, and tracking changes with dates. |
| Using vague goals | Include concrete targets, regions, channels, or owner names where possible. |

## What Business Context changes

Business Context does not connect data by itself. It changes interpretation.

For example, if you ask:

```text
Why did conversion rate fall last week?
```

The AI can combine live GA4, paid media, Search Console, audit findings, KPI definitions, and this context to avoid treating every dip as a tracking bug or every spike as a win.
