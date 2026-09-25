"""
Rule Book — Google Analytics 4 (Ecommerce Events)
====================================================
Spec version: GA4/2024-11
Docs: https://developers.google.com/analytics/devguides/collection/ga4/reference/events

Covers the full GA4 ecommerce event taxonomy: view_item, add_to_cart,
view_cart, begin_checkout, add_payment_info, add_shipping_info, purchase,
refund, and supporting list events.
"""

from app.tag_testing.rule_books.base import (
    EventSpec,
    GlobalRule,
    ParamSpec,
    RuleBook,
)

RULE_BOOK = RuleBook(
    platform="ga4",
    display_name="Google Analytics 4 (Ecommerce)",
    spec_version="GA4/2024-11",
    docs_url="https://developers.google.com/analytics/devguides/collection/ga4/reference/events",
    gtm_type_codes=("gaawc", "gaawe", "googtag"),
    detection_patterns=(r"gtag\s*\(\s*['\"]event['\"]",),
    name_prefix_hints=("ga4 ", "ga ", "google analytics "),
    events=(
        # ── Purchase ────────────────────────────────────────────────────────
        EventSpec(
            event_name="purchase",
            notes="Fired when a transaction is completed.",
            required_params=(
                ParamSpec(
                    "transaction_id",
                    "string",
                    "Unique transaction ID. Must be unique per purchase.",
                    required=True,
                ),
                ParamSpec(
                    "currency",
                    "string",
                    "ISO 4217 currency code (e.g. USD).",
                    required=True,
                    regex=r"^[A-Z]{3}$",
                ),
                ParamSpec(
                    "value", "number", "Total monetary value. Must be >= 0.", required=True, min_value=0
                ),
                ParamSpec("items", "array", "Array of item objects. Must not be empty.", required=True),
            ),
            recommended_params=(
                ParamSpec("coupon", "string", "Coupon code applied."),
                ParamSpec("tax", "number", "Tax amount."),
                ParamSpec("shipping", "number", "Shipping cost."),
                ParamSpec("affiliation", "string", "Store/affiliate name."),
            ),
        ),
        # ── Refund ──────────────────────────────────────────────────────────
        EventSpec(
            event_name="refund",
            notes="Fired when a refund is issued.",
            required_params=(
                ParamSpec("transaction_id", "string", "Unique transaction ID.", required=True),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total monetary value refunded.", required=True, min_value=0),
            ),
            recommended_params=(ParamSpec("items", "array", "Array of refunded item objects."),),
        ),
        # ── Begin Checkout ───────────────────────────────────────────────────
        EventSpec(
            event_name="begin_checkout",
            notes="Fired when a user begins the checkout process.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total value of items.", required=True, min_value=0),
            ),
            recommended_params=(
                ParamSpec("items", "array", "Array of item objects."),
                ParamSpec("coupon", "string", "Coupon code."),
            ),
        ),
        # ── Add to Cart ──────────────────────────────────────────────────────
        EventSpec(
            event_name="add_to_cart",
            notes="Fired when a user adds an item to the shopping cart.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Value of items added.", required=True, min_value=0),
                ParamSpec("items", "array", "Array of item objects.", required=True),
            ),
        ),
        # ── Remove from Cart ─────────────────────────────────────────────────
        EventSpec(
            event_name="remove_from_cart",
            notes="Fired when a user removes an item from the cart.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Value of items removed.", required=True, min_value=0),
                ParamSpec("items", "array", "Array of item objects.", required=True),
            ),
        ),
        # ── View Item ────────────────────────────────────────────────────────
        EventSpec(
            event_name="view_item",
            notes="Fired when a user views an item or item detail page.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Value of the item.", required=True, min_value=0),
                ParamSpec("items", "array", "Array of item objects.", required=True),
            ),
        ),
        # ── View Item List ───────────────────────────────────────────────────
        EventSpec(
            event_name="view_item_list",
            notes="Fired when a list of items is displayed (category page, search results).",
            recommended_params=(
                ParamSpec("item_list_id", "string", "ID for the list."),
                ParamSpec("item_list_name", "string", "Name for the list."),
                ParamSpec("items", "array", "Array of item objects."),
            ),
        ),
        # ── Select Item ──────────────────────────────────────────────────────
        EventSpec(
            event_name="select_item",
            notes="Fired when an item is selected from a list.",
            required_params=(
                ParamSpec("items", "array", "Array of item objects (just the selected one).", required=True),
            ),
            recommended_params=(
                ParamSpec("item_list_id", "string", "ID for the list."),
                ParamSpec("item_list_name", "string", "Name for the list."),
            ),
        ),
        # ── Add Payment Info ─────────────────────────────────────────────────
        EventSpec(
            event_name="add_payment_info",
            notes="Fired when a user submits their payment information.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total value.", required=True, min_value=0),
                ParamSpec("payment_type", "string", "Payment method selected.", required=True),
            ),
            recommended_params=(
                ParamSpec("items", "array", "Array of item objects."),
                ParamSpec("coupon", "string", "Coupon code."),
            ),
        ),
        # ── Add Shipping Info ────────────────────────────────────────────────
        EventSpec(
            event_name="add_shipping_info",
            notes="Fired when a user provides shipping information.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total value.", required=True, min_value=0),
                ParamSpec("shipping_tier", "string", "Shipping tier selected.", required=True),
            ),
            recommended_params=(
                ParamSpec("items", "array", "Array of item objects."),
                ParamSpec("coupon", "string", "Coupon code."),
            ),
        ),
        # ── View Cart ────────────────────────────────────────────────────────
        EventSpec(
            event_name="view_cart",
            notes="Fired when a user views their shopping cart.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total cart value.", required=True, min_value=0),
            ),
            recommended_params=(ParamSpec("items", "array", "Array of item objects."),),
        ),
        # ── Add to Wishlist ───────────────────────────────────────────────────
        EventSpec(
            event_name="add_to_wishlist",
            notes="Fired when a user adds an item to a wishlist.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Value of items.", required=True, min_value=0),
                ParamSpec("items", "array", "Array of item objects.", required=True),
            ),
        ),
        # ── Promotion events ─────────────────────────────────────────────────
        EventSpec(
            event_name="view_promotion",
            notes="Fired when a promotion is viewed.",
            recommended_params=(
                ParamSpec("promotion_id", "string", "ID of the promotion."),
                ParamSpec("promotion_name", "string", "Name of the promotion."),
                ParamSpec("creative_name", "string", "Creative name."),
                ParamSpec("creative_slot", "string", "Creative slot."),
                ParamSpec("items", "array", "Array of associated item objects."),
            ),
        ),
        EventSpec(
            event_name="select_promotion",
            notes="Fired when a promotion is selected.",
            recommended_params=(
                ParamSpec("promotion_id", "string", "ID of the promotion."),
                ParamSpec("promotion_name", "string", "Name of the promotion."),
                ParamSpec("items", "array", "Array of associated item objects."),
            ),
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="ga4.ecom.measurement_id",
            description="GA4 Measurement ID must be present in the configuration tag.",
            severity="critical",
            remediation="Ensure the GA4 Configuration tag has a valid Measurement ID (G-XXXXXXXX).",
            detection_hint="G-",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="ga4.ecom.debug_mode_not_in_prod",
            description="debug_mode or debug_view should not be enabled in production.",
            severity="warning",
            remediation="Remove debug_mode=true from production GA4 tag parameters.",
            detection_hint="debug_mode",
            must_be_present=False,
        ),
    ),
)
