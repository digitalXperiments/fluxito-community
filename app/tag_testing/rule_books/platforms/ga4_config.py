"""
Rule Book — GA4 Configuration (Global Rules Only)
===================================================
Spec version: GA4/2024-11
Covers GTM container-level configuration checks for GA4:
correct measurement ID format, no debug mode in prod, stream config, etc.
"""

from app.tag_testing.rule_books.base import GlobalRule, RuleBook

RULE_BOOK = RuleBook(
    platform="ga4_config",
    display_name="Google Analytics 4 (Configuration)",
    spec_version="GA4/2024-11",
    docs_url="https://support.google.com/analytics/answer/9304153",
    gtm_type_codes=("gaawc", "googtag"),
    detection_patterns=(),
    name_prefix_hints=("ga4 config", "ga4 configuration", "google tag"),
    events=(),
    global_rules=(
        GlobalRule(
            rule_id="ga4.config.measurement_id_format",
            description="GA4 Measurement ID must be in G-XXXXXXXXXX format.",
            severity="critical",
            remediation="Set the GA4 Measurement ID to the format G-XXXXXXXXXX found in your GA4 property data streams.",
            detection_hint="G-",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="ga4.config.no_debug_mode_prod",
            description="debug_mode must not be enabled in production.",
            severity="warning",
            remediation="Remove debug_mode=1 or debug_mode=true from the production GA4 Configuration tag.",
            detection_hint="debug_mode",
            must_be_present=False,
        ),
        GlobalRule(
            rule_id="ga4.config.no_send_page_view_duplicate",
            description="send_page_view should be reviewed to prevent duplicate page view events.",
            severity="info",
            remediation=(
                "If you are manually firing page_view events via GTM, set send_page_view=false "
                "on the GA4 Configuration tag to avoid double-counting."
            ),
            detection_hint="send_page_view",
            must_be_present=False,
        ),
    ),
)
