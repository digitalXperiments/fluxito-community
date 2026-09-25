# Fluxito skill

An [Agent Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills) that
teaches a model how to **operate the Fluxito MCP** well — audits and
analytics/warehouse/ad queries.

## What it is (and isn't)

The Fluxito **MCP server** is self-describing: every tool lists its actions, exposes an
`action="describe"` discovery call, and returns self-describing errors that name the exact
params you're missing. So **this skill does not duplicate per-action parameters** — the
server owns those, and any client (including non-Claude ones) can read them live.

What the skill adds is the part a tool description can't: **method and judgment** — which
audits to run and how to read findings, and how to sequence multi-step work.

## Prerequisite

The Fluxito MCP connector must be connected with an **active project**. The skill checks
this first (`references/mcp-operating-guide.md`).

## Install

- **Claude Code** — copy `fluxito/` into `.claude/skills/` (project) or
  `~/.claude/skills/` (personal).
- **claude.ai / Claude Desktop** — add it as a Skill in settings where your plan supports
  Skills.
- **Other Agent-Skills-compatible tools** — point the tool at the `fluxito/` directory;
  it reads `SKILL.md` and loads `references/` on demand.

## Layout

```
fluxito-skills/
├── README.md
└── fluxito/
    ├── SKILL.md                              # entry point: self-describing-server model,
    │                                         #   intent router, universal hard rules
    ├── references/
    │   ├── mcp-operating-guide.md            # connect, project, tool-surface map, the
    │   │                                     #   describe/error contract, scopes, run_script
    │   └── workflows/
    │       └── audit.md                       # audit & diagnose tracking / conversions
```

Progressive disclosure: `SKILL.md` routes by intent; the model loads only the one
reference it needs.
