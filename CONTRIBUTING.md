# Contributing to Fluxito

Thanks for considering it. Fluxito is a small project; contributions are welcome but please **open an issue first** if you're planning anything beyond a typo or a small bug fix. That saves you from reworking a PR after review.

## What we accept

- Bug fixes with a reproducer in the description
- New platform connectors (open an issue first to confirm scope)
- Tutorial improvements — current screenshots / corrected dev-console wording
- Documentation fixes
- Test coverage for under-tested modules

## What we usually decline

- Cosmetic refactors with no behaviour change
- New top-level features without a prior issue / discussion
- Changes that broaden the surface area without a clear self-host use case
- Style-only changes (we run `ruff` — let it handle style)

## Workflow

1. Fork the repo.
2. Create a branch off `main`.
3. Make your change. Add or update tests if behaviour changed.
4. Run the local check sweep:
   ```bash
   tox       # ruff + mypy + pytest
   ```
5. Open a PR against `main` and link the issue.

## PR review

- A maintainer reviews PRs as time allows. There's no formal SLA.
- All PRs require maintainer approval before merge — this is enforced via [CODEOWNERS](.github/CODEOWNERS) + GitHub branch protection.
- CI must be green.

## Code of Conduct

We adopt the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Be civil; report issues via [GitHub Security Advisories](https://github.com/digitalXperiments/fluxito-community/security/advisories/new).

## Security

Vulnerabilities go to GitHub Security Advisories — never a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree your work is licensed under [Apache 2.0](LICENSE).
