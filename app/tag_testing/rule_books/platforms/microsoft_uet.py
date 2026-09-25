"""
Rule Book — Microsoft Universal Event Tracking (UET)
=====================================================
Spec version: MicrosoftAds/2024-10
Docs: https://help.ads.microsoft.com/apex/index/3/en/56913
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="microsoft_uet",
    display_name="Microsoft UET (Bing Ads)",
    spec_version="MicrosoftAds/2024-10",
    docs_url="https://help.ads.microsoft.com/apex/index/3/en/56913",
    gtm_type_codes=(),
    detection_patterns=(
        r"bat\.bing\.com/action",
        r"bat\.bing\.com/bat",
        r"\buetq\b",
    ),
    name_prefix_hints=("bing ", "microsoft ", "uet "),
    events=(
        EventSpec(
            event_name="purchase",
            notes="Fired when a purchase is completed.",
            required_params=(
                ParamSpec("revenue", "number", "Purchase revenue.", required=True, min_value=0),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
            ),
            recommended_params=(
                ParamSpec("transaction_id", "string", "Unique transaction ID."),
                ParamSpec("items", "array", "Array of item objects."),
            ),
        ),
        EventSpec(
            event_name="add_to_cart",
            notes="Fired when an item is added to the cart.",
            recommended_params=(
                ParamSpec("revenue", "number", "Item value."),
                ParamSpec("currency", "string", "ISO 4217 code."),
            ),
        ),
        EventSpec(event_name="begin_checkout", notes="Checkout started."),
        EventSpec(event_name="generate_lead", notes="Lead generated."),
        EventSpec(event_name="view_item", notes="Product detail viewed."),
        EventSpec(event_name="sign_up", notes="User signed up."),
    ),
    global_rules=(
        GlobalRule(
            rule_id="uet.tag_id_present",
            description="Microsoft UET tag ID must be present in the tag.",
            severity="critical",
            remediation="Set the UET tag ID in the GTM tag parameters.",
            detection_hint="uetq",
            must_be_present=True,
        ),
    ),
)
