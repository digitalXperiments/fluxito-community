"""
Rule Book — Criteo Tag
=======================
Spec version: Criteo/2024-09
Docs: https://help.criteo.com/kb/guide/en/all-criteo-onetag-events-and-parameters-vZbzbEeX86/Steps/775825
"""

from app.tag_testing.rule_books.base import EventSpec, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="criteo",
    display_name="Criteo OneTag",
    spec_version="Criteo/2024-09",
    docs_url="https://help.criteo.com/kb/guide/en/all-criteo-onetag-events-and-parameters-vZbzbEeX86/Steps/775825",
    gtm_type_codes=(),
    detection_patterns=(
        r"static\.criteo\.net",
        r"Criteo\.events\.",
        r"\bcriteo_q\b",
    ),
    name_prefix_hints=("criteo ",),
    events=(
        EventSpec(
            event_name="trackTransaction",
            notes="Fired when a purchase is completed.",
            required_params=(
                ParamSpec("id", "string", "Unique transaction ID.", required=True),
                ParamSpec("items", "array", "Array of {id,price,quantity} objects.", required=True),
            ),
            recommended_params=(
                ParamSpec("revenue", "number", "Total revenue."),
                ParamSpec("currency", "string", "ISO 4217 code."),
                ParamSpec("customer", "object", "Customer object for enhanced matching."),
            ),
        ),
        EventSpec(
            event_name="viewItem",
            notes="Fired when a product is viewed.",
            required_params=(ParamSpec("item", "string", "Product ID.", required=True),),
        ),
        EventSpec(
            event_name="addToCart",
            notes="Fired when an item is added to the cart.",
            required_params=(
                ParamSpec("items", "array", "Array of {id,price,quantity} objects.", required=True),
            ),
        ),
        EventSpec(
            event_name="viewList",
            notes="Fired when a product list is viewed.",
            recommended_params=(
                ParamSpec("item", "array", "Array of product IDs."),
                ParamSpec("category", "string", "Category name."),
                ParamSpec("keywords", "string", "Search keywords."),
            ),
        ),
        EventSpec(event_name="viewHome", notes="Home page view."),
        EventSpec(event_name="viewBasket", notes="Cart/basket view."),
    ),
    global_rules=(),
)
