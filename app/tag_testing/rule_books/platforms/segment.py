"""
Rule Book — Segment Analytics.js
==================================
Spec version: Segment/2024-11
Docs: https://segment.com/docs/connections/spec/ecommerce/v2/
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="segment",
    display_name="Segment Analytics.js",
    spec_version="Segment/2024-11",
    docs_url="https://segment.com/docs/connections/spec/ecommerce/v2/",
    gtm_type_codes=(),
    detection_patterns=(
        r"cdn\.segment\.com",
        r"analytics\.track\s*\(",
        r"window\.analytics\b",
        r"analytics\.js",
    ),
    name_prefix_hints=("segment ", "analytics.js "),
    events=(
        EventSpec(
            event_name="Order Completed",
            notes="Fired when an order is completed.",
            required_params=(
                ParamSpec("order_id", "string", "Unique order ID.", required=True),
                ParamSpec("total", "number", "Order total.", required=True, min_value=0),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("products", "array", "Array of product objects.", required=True),
            ),
            recommended_params=(
                ParamSpec("revenue", "number", "Revenue (excl. tax, shipping)."),
                ParamSpec("tax", "number", "Tax amount."),
                ParamSpec("shipping", "number", "Shipping cost."),
                ParamSpec("coupon", "string", "Coupon code."),
            ),
        ),
        EventSpec(
            event_name="Product Added",
            notes="Fired when a product is added to the cart.",
            required_params=(
                ParamSpec("product_id", "string", "Product ID.", required=True),
                ParamSpec("name", "string", "Product name.", required=True),
                ParamSpec("price", "number", "Product price.", required=True, min_value=0),
                ParamSpec("quantity", "integer", "Quantity.", required=True),
            ),
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("sku", "string", "Product SKU."),
                ParamSpec("category", "string", "Product category."),
            ),
        ),
        EventSpec(
            event_name="Product Viewed",
            notes="Fired when a product detail page is viewed.",
            required_params=(
                ParamSpec("product_id", "string", "Product ID.", required=True),
                ParamSpec("name", "string", "Product name.", required=True),
                ParamSpec("price", "number", "Product price.", required=True, min_value=0),
            ),
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("category", "string", "Product category."),
            ),
        ),
        EventSpec(
            event_name="Checkout Started",
            notes="Fired when a user initiates checkout.",
            required_params=(
                ParamSpec("order_id", "string", "Order ID.", required=True),
                ParamSpec("total", "number", "Checkout total.", required=True, min_value=0),
                ParamSpec("currency", "string", "ISO 4217 code.", required=True, regex=r"^[A-Z]{3}$"),
                ParamSpec("products", "array", "Array of products.", required=True),
            ),
        ),
        EventSpec(
            event_name="Payment Info Entered",
            notes="Fired when payment info is entered.",
            recommended_params=(
                ParamSpec("order_id", "string", "Order ID."),
                ParamSpec("payment_method", "string", "Payment method."),
            ),
        ),
        EventSpec(
            event_name="Product List Viewed",
            notes="Fired when a product list is viewed.",
            required_params=(
                ParamSpec("list_id", "string", "Product list ID.", required=True),
                ParamSpec("category", "string", "Product category.", required=True),
                ParamSpec("products", "array", "Array of products.", required=True),
            ),
        ),
        EventSpec(event_name="Product Clicked", notes="Product clicked from list."),
        EventSpec(event_name="Product Removed", notes="Product removed from cart."),
        EventSpec(event_name="Cart Viewed", notes="Cart page viewed."),
        EventSpec(event_name="Promotion Viewed", notes="Promotion viewed."),
        EventSpec(event_name="Promotion Clicked", notes="Promotion clicked."),
        EventSpec(event_name="Signed Up", notes="User signed up."),
        EventSpec(event_name="Signed In", notes="User signed in."),
        EventSpec(event_name="Signed Out", notes="User signed out."),
        EventSpec(event_name="Page", notes="Page view (Segment page() call)."),
        EventSpec(event_name="Identify", notes="User identification call."),
    ),
    global_rules=(
        GlobalRule(
            rule_id="segment.write_key_present",
            description="Segment write key must be present in the analytics.js initialization.",
            severity="critical",
            remediation="Initialize Segment with your write key: analytics.load('YOUR_WRITE_KEY').",
            detection_hint="analytics.load(",
            must_be_present=True,
        ),
    ),
)
