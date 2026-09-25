"""
Rule Book — Google Ads Conversion Tracking
===========================================
Spec version: GoogleAds/2024-11
Docs: https://support.google.com/google-ads/answer/6095821
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="google_ads_conversion",
    display_name="Google Ads Conversion Tracking",
    spec_version="GoogleAds/2024-11",
    docs_url="https://support.google.com/google-ads/answer/6095821",
    gtm_type_codes=("awct", "gclidw"),
    detection_patterns=(r"googleadservices\.com/pagead/conversion", r"gtag.*AW-"),
    name_prefix_hints=("google ads conversion", "gads conv", "awct"),
    events=(
        EventSpec(
            event_name="conversion",
            notes="Google Ads conversion event.",
            required_params=(
                ParamSpec(
                    "send_to",
                    "string",
                    "Conversion ID and label in format AW-XXXXXXXXX/LABEL.",
                    required=True,
                ),
                ParamSpec("value", "number", "Conversion value.", required=True, min_value=0),
                ParamSpec("currency", "string", "ISO 4217 code.", required=True, regex=r"^[A-Z]{3}$"),
                ParamSpec(
                    "transaction_id", "string", "Unique transaction ID for deduplication.", required=True
                ),
            ),
            recommended_params=(
                ParamSpec("new_customer", "boolean", "Whether this is a new customer conversion."),
            ),
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="gads.conversion.no_test_send_to",
            description="send_to must not contain a placeholder or test conversion ID.",
            severity="critical",
            remediation="Replace the placeholder conversion ID with the actual AW-XXXXXXXXX/LABEL value.",
            detection_hint="AW-REPLACE",
            must_be_present=False,
        ),
    ),
)
