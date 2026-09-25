"""
Rule Book — Google Ads Remarketing
====================================
Spec version: GoogleAds/2024-11
Docs: https://support.google.com/google-ads/answer/3103695
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="google_ads_remarketing",
    display_name="Google Ads Remarketing",
    spec_version="GoogleAds/2024-11",
    docs_url="https://support.google.com/google-ads/answer/3103695",
    gtm_type_codes=("awrd",),
    detection_patterns=(r"googleadservices\.com/pagead/remarketing",),
    name_prefix_hints=("google ads remarketing", "gads remarketing", "awrd"),
    events=(
        EventSpec(
            event_name="page_view",
            notes="Remarketing page view — fires on every page to build remarketing lists.",
            recommended_params=(
                ParamSpec("ecomm_prodid", "array", "Product IDs on the page (for dynamic remarketing)."),
                ParamSpec(
                    "ecomm_pagetype",
                    "string",
                    "Page type: home, searchresults, category, product, cart, purchase.",
                ),
                ParamSpec("ecomm_totalvalue", "number", "Total value of products on the page."),
            ),
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="gads.remarketing.conversion_id_present",
            description="Google Ads Conversion ID must be present in the remarketing tag.",
            severity="critical",
            remediation="Set the Conversion ID (AW-XXXXXXXXX) in the GTM remarketing tag parameters.",
            detection_hint="AW-",
            must_be_present=True,
        ),
    ),
)
