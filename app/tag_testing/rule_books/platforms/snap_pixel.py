"""
Rule Book — Snap Pixel
=======================
Spec version: Snap/2024-09
Docs: https://businesshelp.snapchat.com/s/article/pixel-direct-implementation
"""

from app.tag_testing.rule_books.base import EventSpec, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="snap_pixel",
    display_name="Snap Pixel",
    spec_version="Snap/2024-09",
    docs_url="https://businesshelp.snapchat.com/s/article/pixel-direct-implementation",
    gtm_type_codes=(),
    detection_patterns=(
        r"sc-static\.net/scevent",
        r"tr\.snapchat\.com",
        r"snaptr\s*\(",
    ),
    name_prefix_hints=("snap ", "snapchat "),
    events=(
        EventSpec(
            event_name="PURCHASE",
            notes="Fired when a purchase is completed.",
            required_params=(
                ParamSpec("price", "number", "Purchase price.", required=True, min_value=0),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("item_ids", "array", "Array of purchased item IDs.", required=True),
                ParamSpec("transaction_id", "string", "Unique transaction ID.", required=True),
            ),
            recommended_params=(
                ParamSpec("number_items", "integer", "Number of items purchased."),
                ParamSpec("item_category", "string", "Product category."),
            ),
        ),
        EventSpec(
            event_name="ADD_CART",
            notes="Fired when an item is added to the cart.",
            required_params=(
                ParamSpec("price", "number", "Item price.", required=True, min_value=0),
                ParamSpec("currency", "string", "ISO 4217 code.", required=True, regex=r"^[A-Z]{3}$"),
                ParamSpec("item_ids", "array", "Array of item IDs.", required=True),
            ),
        ),
        EventSpec(
            event_name="START_CHECKOUT",
            notes="Fired when a user begins the checkout process.",
            required_params=(
                ParamSpec("price", "number", "Total checkout value.", required=True, min_value=0),
                ParamSpec("currency", "string", "ISO 4217 code.", required=True, regex=r"^[A-Z]{3}$"),
            ),
            recommended_params=(
                ParamSpec("item_ids", "array", "Array of item IDs."),
                ParamSpec("number_items", "integer", "Number of items."),
            ),
        ),
        EventSpec(
            event_name="VIEW_CONTENT",
            notes="Fired when a user views a product detail page.",
            required_params=(
                ParamSpec("price", "number", "Product price.", required=True, min_value=0),
                ParamSpec("currency", "string", "ISO 4217 code.", required=True, regex=r"^[A-Z]{3}$"),
                ParamSpec("item_ids", "array", "Array of item IDs.", required=True),
            ),
        ),
        EventSpec(event_name="PAGE_VIEW", notes="Base pixel page view."),
        EventSpec(event_name="SIGN_UP", notes="User registers."),
        EventSpec(event_name="APP_INSTALL", notes="App install."),
        EventSpec(event_name="APP_OPEN", notes="App open."),
        EventSpec(event_name="LEVEL_COMPLETE", notes="Game level complete."),
        EventSpec(event_name="SAVE", notes="Content saved."),
        EventSpec(event_name="SEARCH", notes="Search performed."),
        EventSpec(event_name="SUBSCRIBE", notes="Subscription."),
        EventSpec(event_name="AD_CLICK", notes="Ad click."),
        EventSpec(event_name="AD_VIEW", notes="Ad view."),
        EventSpec(event_name="COMPLETE_TUTORIAL", notes="Tutorial completed."),
        EventSpec(event_name="INVITE", notes="User sent an invite."),
        EventSpec(event_name="LOGIN", notes="User logged in."),
        EventSpec(event_name="RESERVE", notes="Reservation made."),
        EventSpec(event_name="ACHIEVEMENT_UNLOCKED", notes="Achievement unlocked."),
        EventSpec(event_name="SPENT_CREDITS", notes="In-app credits spent."),
        EventSpec(event_name="RATE", notes="Content rated."),
    ),
    global_rules=(),
)
