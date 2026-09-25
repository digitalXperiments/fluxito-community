"""Rule Book — PostHog"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, RuleBook

RULE_BOOK = RuleBook(
    platform="posthog",
    display_name="PostHog",
    spec_version="PostHog/2024-09",
    docs_url="https://posthog.com/docs/product-analytics/installation",
    gtm_type_codes=(),
    detection_patterns=(
        r"posthog\.init",
        r"posthog\.capture",
        r"posthog\.identify",
        r"app\.posthog\.com",
        r"t\.posthog\.com",
    ),
    name_prefix_hints=("posthog ",),
    events=(
        EventSpec(
            event_name="$pageview",
            notes="Auto-captured by PostHog on init",
        ),
        EventSpec(
            event_name="pageview",
            notes="Manual pageview capture",
        ),
        EventSpec(
            event_name="identify",
            notes="User identification via posthog.identify()",
        ),
        EventSpec(
            event_name="$identify",
            notes="PostHog's internal identify event",
        ),
        EventSpec(
            event_name="purchase",
            notes="Revenue event — should use posthog.capture with $set revenue property",
        ),
        EventSpec(
            event_name="custom_event",
            notes="Generic custom event captured via posthog.capture()",
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="posthog.project_api_key_present",
            description="PostHog must be initialized with a project API key",
            severity="critical",
            remediation="Initialize PostHog with your project API key: posthog.init('YOUR_API_KEY', { api_host: 'https://app.posthog.com' })",
            detection_hint="posthog.init(",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="posthog.no_debug_in_prod",
            description="Debug mode should be off in production",
            severity="warning",
            remediation="Remove posthog.debug(true) or set it to false for production builds.",
            detection_hint="posthog.debug",
            must_be_present=False,
        ),
        GlobalRule(
            rule_id="posthog.autocapture_enabled",
            description="Autocapture should be explicitly configured",
            severity="info",
            remediation="PostHog autocapture is on by default; verify it matches your tracking plan.",
            detection_hint=None,
            must_be_present=None,
        ),
    ),
)
