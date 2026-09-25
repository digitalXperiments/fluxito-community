# Changelog

All notable changes to Fluxito Community will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-09-25

The first release of **Fluxito Community**: an open-source MCP server for marketing analytics, with tracking audits and a simple built-in chat. It's a stable snapshot that gets security fixes only. New features ship on [Fluxito Cloud](https://fluxito.app).

### Added
- **MCP server for 27 platforms.** GA4, GTM, Google Ads, Search Console, Bing, BigQuery, Snowflake, Redshift, Meta, TikTok, LinkedIn, Snap, X, Reddit, Pinterest, Apple Ads, Amplitude, Mixpanel, PostHog, Adobe Analytics, Adobe Launch, Marketo, Adjust, AppsFlyer, Branch, Braze and MoEngage, behind one `/mcp` endpoint.
- **Audit.** Tag QA against 21 platform rule books, live tag tests, vendors and custom rules, and scheduled test flows with email and Slack alerts.
- **Context.** A KPI library (with computed values) and a business context document your AI reads.
- **Chat.** A built-in chat that uses your own OpenAI-compatible API key. Set a Base URL, model and key under **Settings → AI providers**, with presets for OpenAI and OpenRouter. It's read-only (it can look at connected platforms but never change them) and sends only the last 20 messages.
- **Teams.** Projects, members, roles (RBAC), per-project OAuth apps, MCP access tokens, and an activity log of every AI tool call.
- **Self-hosting.** Docker Compose images (`fluxito-community`, `fluxito-community-updater`, `fluxito-community-nginx`) with one-click in-app updates.
