"""
Rule Book — Twitter/X Pixel
============================
Spec version: Twitter/2024-08
Docs: https://business.twitter.com/en/help/campaign-measurement-and-analytics/conversion-tracking-for-websites.html
"""

from app.tag_testing.rule_books.base import EventSpec, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="twitter_pixel",
    display_name="Twitter/X Pixel",
    spec_version="Twitter/2024-08",
    docs_url="https://business.twitter.com/en/help/campaign-measurement-and-analytics/conversion-tracking-for-websites.html",
    gtm_type_codes=(),
    detection_patterns=(
        r"static\.ads-twitter\.com",
        r"twq\s*\(",
        r"twitter.*pixel",
    ),
    name_prefix_hints=("twitter ", "x ", "twq "),
    events=(
        EventSpec(
            event_name="Purchase",
            notes="Fired when a purchase is completed.",
            required_params=(
                ParamSpec("value", "number", "Purchase value.", required=True, min_value=0),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("conversion_id", "string", "Twitter conversion ID.", required=True),
            ),
            recommended_params=(
                ParamSpec("num_items", "integer", "Number of items."),
                ParamSpec("order_id", "string", "Order ID."),
                ParamSpec("content_ids", "array", "Product IDs."),
            ),
        ),
        EventSpec(
            event_name="AddToCart",
            notes="Fired when an item is added to the cart.",
            required_params=(
                ParamSpec("value", "number", "Value of items.", required=True, min_value=0),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("conversion_id", "string", "Twitter conversion ID.", required=True),
            ),
        ),
        EventSpec(
            event_name="InitiateCheckout",
            notes="Fired when a user starts checkout.",
            required_params=(
                ParamSpec("value", "number", "Checkout value.", required=True, min_value=0),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("conversion_id", "string", "Twitter conversion ID.", required=True),
            ),
        ),
        EventSpec(event_name="Search", notes="Search performed."),
        EventSpec(event_name="ViewContent", notes="Product viewed."),
        EventSpec(event_name="Lead", notes="Lead generated."),
        EventSpec(event_name="Download", notes="Download completed."),
    ),
    global_rules=(),
)
