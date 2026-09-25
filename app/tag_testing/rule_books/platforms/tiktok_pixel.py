"""
Rule Book — TikTok Pixel
=========================
Spec version: TikTok/2024-09
Docs: https://ads.tiktok.com/marketing_api/docs?id=1701890979375106

TikTok Pixel uses PascalCase event names similar to Meta Pixel.
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="tiktok_pixel",
    display_name="TikTok Pixel",
    spec_version="TikTok/2024-09",
    docs_url="https://ads.tiktok.com/marketing_api/docs?id=1701890979375106",
    gtm_type_codes=(),
    detection_patterns=(
        r"analytics\.tiktok\.com",
        r"ttq\.track\s*\(",
        r"ttq\.load\s*\(",
        r"TiktokAnalyticsObject",
    ),
    name_prefix_hints=("tiktok ", "ttk "),
    events=(
        EventSpec(
            event_name="Purchase",
            notes="Fired when a purchase transaction completes.",
            required_params=(
                ParamSpec("content_id", "string", "ID of the product purchased.", required=True),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total purchase value.", required=True, min_value=0),
                ParamSpec(
                    "content_type",
                    "string",
                    "'product' or 'product_group'.",
                    required=True,
                    allowed_values=("product", "product_group"),
                ),
            ),
            recommended_params=(
                ParamSpec("quantity", "integer", "Total quantity purchased."),
                ParamSpec("content_name", "string", "Product name."),
                ParamSpec("order_id", "string", "Order ID for deduplication."),
            ),
        ),
        EventSpec(
            event_name="AddToCart",
            notes="Fired when an item is added to the cart.",
            required_params=(
                ParamSpec("content_id", "string", "Product ID.", required=True),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Value of item(s) added.", required=True, min_value=0),
                ParamSpec(
                    "content_type",
                    "string",
                    "'product' or 'product_group'.",
                    required=True,
                    allowed_values=("product", "product_group"),
                ),
            ),
            recommended_params=(
                ParamSpec("quantity", "integer", "Quantity added."),
                ParamSpec("content_name", "string", "Product name."),
            ),
        ),
        EventSpec(
            event_name="InitiateCheckout",
            notes="Fired when a user begins checkout.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total checkout value.", required=True, min_value=0),
            ),
            recommended_params=(
                ParamSpec("content_id", "string", "Product IDs."),
                ParamSpec("content_type", "string", "'product' or 'product_group'."),
                ParamSpec("quantity", "integer", "Total quantity."),
            ),
        ),
        EventSpec(
            event_name="ViewContent",
            notes="Fired when a user views a product detail page.",
            required_params=(
                ParamSpec("content_id", "string", "Product ID.", required=True),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Product value.", required=True, min_value=0),
                ParamSpec(
                    "content_type",
                    "string",
                    "'product' or 'product_group'.",
                    required=True,
                    allowed_values=("product", "product_group"),
                ),
            ),
            recommended_params=(ParamSpec("content_name", "string", "Product name."),),
        ),
        EventSpec(
            event_name="PlaceAnOrder",
            notes="Fired when a purchase order is placed.",
            required_params=(
                ParamSpec("content_id", "string", "Product ID.", required=True),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Order value.", required=True, min_value=0),
            ),
        ),
        EventSpec(
            event_name="CompletePayment",
            notes="Fired when a payment is completed.",
            required_params=(
                ParamSpec("content_id", "string", "Product ID.", required=True),
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Payment value.", required=True, min_value=0),
            ),
        ),
        EventSpec(
            event_name="AddPaymentInfo",
            notes="Fired when a user submits payment info.",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Cart value."),
            ),
        ),
        EventSpec(
            event_name="Search",
            notes="Fired when a user searches.",
            recommended_params=(
                ParamSpec("query", "string", "Search query."),
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Estimated value."),
            ),
        ),
        EventSpec(event_name="Contact", notes="User contacts the business."),
        EventSpec(event_name="Download", notes="User downloads content."),
        EventSpec(event_name="SubmitForm", notes="User submits a form."),
        EventSpec(event_name="Subscribe", notes="User subscribes to a service."),
        EventSpec(event_name="Registration", notes="User completes registration."),
        EventSpec(
            event_name="PageView",
            notes="Base pixel page view.",
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="tiktok.pixel.test_event_code",
            description="Test event code must not be present in production tags.",
            severity="critical",
            remediation="Remove the test event code from the production TikTok Pixel tag.",
            detection_hint="test_event_code",
            must_be_present=False,
        ),
    ),
)
