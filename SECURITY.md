# Security Policy

## Supported Versions

We support the latest minor release line. Security fixes land on `main` and the most recent minor branch.

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |
| < 1.0   | No        |

## Reporting a Vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Report privately by opening a GitHub Security Advisory at:
https://github.com/digitalXperiments/fluxito-community/security/advisories/new

Please open a GitHub Security Advisory for private disclosure. If you must email, use a temporary address or reach out via the repo's issues first.

You can expect:

- An acknowledgement within 3 business days
- A triage update within 7 business days
- A fix or mitigation plan within 30 days for confirmed high/critical issues, faster if actively exploited

## Scope

**In scope:**

- Authentication or authorization bypass
- Token-handling defects (Fernet key compromise paths, leaked credentials in logs or API responses)
- SQL injection, command injection, SSRF in connectors
- Cross-site request forgery on state-changing endpoints
- Privilege escalation across project members

**Out of scope:**

- Issues on third-party platforms (Google, Meta, TikTok, Snowflake, etc.) — report those directly to the respective vendor
- Self-hosters running heavily-modified forks where the issue does not reproduce on unmodified `main`
- Theoretical issues with no demonstrated exploit path
- Denial-of-service from resource-exhaustion without an authentication bypass component
