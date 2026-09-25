# Add the Fluxito Skill

The Fluxito MCP gives your AI **hands and facts** — it can read your analytics, query your warehouses and ad platforms, and run audits. The **Fluxito Skill** gives your AI the **operating manual** for those tools: the method, the document contract, and the guardrails that keep results gold-standard instead of generic.

Connecting the MCP is enough to *call* the tools. Adding the Skill is what makes the AI *use them well*.

## Why add the Skill to your AI client

Without the Skill, an AI connected to Fluxito will improvise — it may invent metrics, call a broken tracking setup "healthy," or guess at tool parameters instead of reading the server's own specs. The Skill fixes that:

- **Right method, every time.** Repeatable workflows for auditing tracking, tags, and data quality that work for any vertical (ecommerce, SaaS, lead-gen, media, marketplace) and any stack (GA4, Adobe, Amplitude, warehouse).
- **Self-describing tools, used properly.** The Skill teaches the AI to read each tool's actions and `action="describe"` specs instead of guessing parameters.
- **Honest health checks.** The Skill forces the AI to read server-computed `findings` before drawing conclusions — no more "looks good" while a critical issue stands.
- **Lean context.** One hub skill with per-feature references loaded on demand, so it never bloats your context window.

## Prerequisite

1. The **Fluxito MCP** connector is added to your AI client. See [Connect an AI with MCP](/tutorials/connect-ai-mcp).
2. You have selected an **active project**. The Skill drives the MCP's audit and analytics tools; without a connected MCP and a project it has nothing to operate.

## Where to get it

The Skill ships in the Fluxito repository under the **`fluxito-skills/`** folder. The skill itself is the `fluxito/` directory inside it:

```text
fluxito-skills/
└── fluxito/
    ├── SKILL.md          # thin router + universal hard rules (always loaded)
    └── references/       # MCP operating guide + workflow guides (loaded on demand)
```

## Install

**Claude Code** — copy the `fluxito` folder into your skills directory:

```bash
cp -r fluxito-skills/fluxito ~/.claude/skills/      # personal (all projects)
cp -r fluxito-skills/fluxito .claude/skills/        # or project-scoped
```

**Claude Desktop / claude.ai (Capabilities)** — upload the `fluxito` folder as a Skill wherever your plan supports Skills.

**Other Agent-Skills-compatible tools** — point the tool at the `fluxito` folder. It reads `SKILL.md` and pulls in `references/` only when a task needs them.

## Verify it works

Start a fresh chat in your AI client (with the Fluxito MCP connected and a project selected) and ask:

> "Audit the GA4 and GTM tracking for my active project and summarize the findings by severity."

If the Skill is installed, the AI will follow the Fluxito method: confirm the active project, read the server-computed audit `findings`, and report on them — rather than free-styling. If it skips those steps, re-check that the `fluxito` folder is in the right skills location and restart the client.

## What's next

- [KPI Library](/tutorials/kpi-library) and [Business Context](/tutorials/business-context) — the project knowledge the Skill tells the AI to use.
- [Audits and Activity Log](/tutorials/audits-and-activity) — review exactly which tools the AI called.
