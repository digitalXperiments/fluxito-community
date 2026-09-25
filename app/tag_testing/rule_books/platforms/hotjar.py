"""Rule Book — Hotjar (configuration global rules only)"""

from app.tag_testing.rule_books.base import GlobalRule, RuleBook

RULE_BOOK = RuleBook(
    platform="hotjar",
    display_name="Hotjar",
    spec_version="Hotjar/2024-09",
    docs_url="https://help.hotjar.com/hc/en-us/articles/115011639927",
    gtm_type_codes=(),
    detection_patterns=(r"static\.hotjar\.com", r"\bhj\s*\(", r"_hjSettings\b"),
    name_prefix_hints=("hotjar ", "hj "),
    events=(),
    global_rules=(
        GlobalRule(
            rule_id="hotjar.site_id_present",
            description="Hotjar Site ID (hjid) must be present in the tag.",
            severity="critical",
            remediation="Set the hjid variable to your Hotjar Site ID in the GTM tag.",
            detection_hint="hjid",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="hotjar.not_on_sensitive_pages",
            description="Hotjar must be excluded from pages with sensitive user data (checkout, account).",
            severity="warning",
            remediation="Add page path exclusion triggers for /checkout and /account pages.",
            detection_hint="",
            must_be_present=False,
        ),
    ),
)
