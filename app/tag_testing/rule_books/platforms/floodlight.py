"""
Rule Book — Floodlight (Campaign Manager 360 / DV360)
======================================================
Spec version: CM360/2024-10
Docs: https://support.google.com/campaignmanager/answer/2583078
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="floodlight",
    display_name="Floodlight (Campaign Manager 360)",
    spec_version="CM360/2024-10",
    docs_url="https://support.google.com/campaignmanager/answer/2583078",
    gtm_type_codes=("fls", "flsactivity", "flsa"),
    detection_patterns=(r"fls\.doubleclick\.net|dc_pre=|gtag.*DC-"),
    name_prefix_hints=("floodlight ", "fls ", "cm360 "),
    events=(
        EventSpec(
            event_name="floodlight_activity",
            notes="Generic Floodlight activity event.",
            required_params=(
                ParamSpec("src", "string", "Advertiser source ID (DC- prefix).", required=True),
                ParamSpec("type", "string", "Activity group tag string.", required=True),
                ParamSpec("cat", "string", "Activity tag string.", required=True),
            ),
            recommended_params=(
                ParamSpec("ord", "string", "Cache-busting / order ID for transaction deduplication."),
                ParamSpec("qty", "number", "Quantity of items purchased."),
                ParamSpec("cost", "number", "Transaction revenue."),
            ),
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="floodlight.src_not_placeholder",
            description="Floodlight src (advertiser ID) must not be a placeholder.",
            severity="critical",
            remediation="Replace the placeholder DC- ID with your actual Campaign Manager advertiser ID.",
            detection_hint="DC-REPLACE",
            must_be_present=False,
        ),
    ),
)
