"""
Rule Book — Pinterest Tag
==========================
Spec version: Pinterest/2024-09
Docs: https://help.pinterest.com/en/business/article/track-conversions-with-the-pinterest-tag
"""

from app.tag_testing.rule_books.base import EventSpec, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="pinterest_tag",
    display_name="Pinterest Tag",
    spec_version="Pinterest/2024-09",
    docs_url="https://help.pinterest.com/en/business/article/track-conversions-with-the-pinterest-tag",
    gtm_type_codes=(),
    detection_patterns=(
        r"pintrk\s*\(",
        r"ct\.pinterest\.com",
        r"s\.pinimg\.com/ct",
    ),
    name_prefix_hints=("pinterest ", "pintrk "),
    events=(
        EventSpec(
            event_name="checkout",
            notes="Fired when a purchase is completed.",
            required_params=(
                ParamSpec("value", "number", "Order value.", required=True, min_value=0),
                ParamSpec("order_quantity", "integer", "Number of items.", required=True),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("line_items", "array", "Array of item objects.", required=True),
            ),
            recommended_params=(
                ParamSpec("order_id", "string", "Unique order ID."),
                ParamSpec("coupon", "string", "Coupon code."),
            ),
        ),
        EventSpec(
            event_name="addtocart",
            notes="Fired when an item is added to the cart.",
            required_params=(
                ParamSpec("value", "number", "Value of items added.", required=True, min_value=0),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("line_items", "array", "Array of item objects.", required=True),
            ),
        ),
        EventSpec(
            event_name="pagevisit",
            notes="Page view / product view.",
            recommended_params=(
                ParamSpec("line_items", "array", "Array of item objects (for product pages)."),
            ),
        ),
        EventSpec(event_name="signup", notes="User signs up."),
        EventSpec(event_name="lead", notes="Lead generated."),
        EventSpec(event_name="search", notes="Search performed."),
        EventSpec(event_name="watchvideo", notes="Video watched."),
        EventSpec(event_name="custom", notes="Custom event."),
    ),
    global_rules=(),
)
