"""
Rule Book — Meta Pixel (Facebook Pixel)
========================================
Spec version: Meta/2024-10
Docs: https://developers.facebook.com/docs/meta-pixel/reference

Covers all standard Meta Pixel events and their required/recommended params.
Note: Meta Pixel uses PascalCase event names (e.g. "Purchase" not "purchase").
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="facebook_pixel",
    display_name="Meta Pixel (Facebook Pixel)",
    spec_version="Meta/2024-10",
    docs_url="https://developers.facebook.com/docs/meta-pixel/reference",
    gtm_type_codes=(),  # No native GTM type — Custom HTML only
    detection_patterns=(
        r"connect\.facebook\.net",
        r"fbevents\.js",
        r"fbq\s*\(",
    ),
    name_prefix_hints=("meta ", "meta-", "fb ", "fb-", "facebook "),
    events=(
        # ── Purchase ────────────────────────────────────────────────────────
        EventSpec(
            event_name="Purchase",
            notes="Fired when a purchase is completed.",
            required_params=(
                ParamSpec(
                    "value",
                    "number",
                    "Monetary value of the purchase. Must be > 0.",
                    required=True,
                    min_value=0,
                ),
                ParamSpec(
                    "currency",
                    "string",
                    "ISO 4217 currency code (e.g. USD).",
                    required=True,
                    regex=r"^[A-Z]{3}$",
                ),
                ParamSpec(
                    "content_ids",
                    "array",
                    "Non-empty array of product IDs. Required for CAPI dedup.",
                    required=True,
                ),
                ParamSpec(
                    "content_type",
                    "string",
                    "Must be 'product' or 'product_group'.",
                    required=True,
                    allowed_values=("product", "product_group"),
                ),
            ),
            recommended_params=(
                ParamSpec("num_items", "integer", "Number of items purchased."),
                ParamSpec("order_id", "string", "Order ID — required for CAPI deduplication."),
                ParamSpec("contents", "array", "Array of {id, quantity, price} objects."),
            ),
        ),
        # ── InitiateCheckout ─────────────────────────────────────────────────
        EventSpec(
            event_name="InitiateCheckout",
            notes="Fired when a user begins the checkout process.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Total value of checkout.", required=True, min_value=0),
                ParamSpec("content_ids", "array", "Array of product IDs.", required=True),
            ),
            recommended_params=(
                ParamSpec("num_items", "integer", "Number of items."),
                ParamSpec("content_type", "string", "'product' or 'product_group'."),
                ParamSpec("contents", "array", "Array of {id, quantity} objects."),
            ),
        ),
        # ── AddToCart ────────────────────────────────────────────────────────
        EventSpec(
            event_name="AddToCart",
            notes="Fired when a user adds an item to the cart.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Value of items added.", required=True, min_value=0),
                ParamSpec("content_ids", "array", "Array of product IDs.", required=True),
            ),
            recommended_params=(
                ParamSpec("content_name", "string", "Name of the product."),
                ParamSpec("content_type", "string", "'product' or 'product_group'."),
                ParamSpec("contents", "array", "Array of {id, quantity} objects."),
            ),
        ),
        # ── ViewContent ──────────────────────────────────────────────────────
        EventSpec(
            event_name="ViewContent",
            notes="Fired when a user views a product detail page.",
            required_params=(
                ParamSpec(
                    "currency", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
                ParamSpec("value", "number", "Value of the viewed content.", required=True, min_value=0),
                ParamSpec("content_ids", "array", "Array of product IDs.", required=True),
            ),
            recommended_params=(
                ParamSpec("content_name", "string", "Name of the product."),
                ParamSpec("content_type", "string", "'product' or 'product_group'."),
                ParamSpec("contents", "array", "Array of {id, quantity} objects."),
            ),
        ),
        # ── Lead ─────────────────────────────────────────────────────────────
        EventSpec(
            event_name="Lead",
            notes="Fired when a lead is generated (form submission, newsletter signup).",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Estimated value of the lead."),
            ),
        ),
        # ── CompleteRegistration ──────────────────────────────────────────────
        EventSpec(
            event_name="CompleteRegistration",
            notes="Fired when a user completes a registration.",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Value attributed to the registration."),
                ParamSpec("status", "string", "Status of the registration."),
            ),
        ),
        # ── AddPaymentInfo ───────────────────────────────────────────────────
        EventSpec(
            event_name="AddPaymentInfo",
            notes="Fired when a user adds payment information.",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Total cart value."),
                ParamSpec("content_ids", "array", "Array of product IDs."),
                ParamSpec("content_type", "string", "'product' or 'product_group'."),
            ),
        ),
        # ── AddToWishlist ────────────────────────────────────────────────────
        EventSpec(
            event_name="AddToWishlist",
            notes="Fired when a user adds an item to a wishlist.",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Value of the wishlisted item."),
                ParamSpec("content_ids", "array", "Array of product IDs."),
                ParamSpec("content_name", "string", "Name of the product."),
            ),
        ),
        # ── Search ───────────────────────────────────────────────────────────
        EventSpec(
            event_name="Search",
            notes="Fired when a user performs a search.",
            recommended_params=(
                ParamSpec("search_string", "string", "The search term."),
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Estimated value of search."),
                ParamSpec("content_ids", "array", "Array of product IDs found."),
            ),
        ),
        # ── PageView ─────────────────────────────────────────────────────────
        EventSpec(
            event_name="PageView",
            notes="Fired on every page view (base pixel initialisation).",
        ),
        # ── Contact ──────────────────────────────────────────────────────────
        EventSpec(
            event_name="Contact",
            notes="Fired when a user contacts the business.",
        ),
        # ── CustomizeProduct ─────────────────────────────────────────────────
        EventSpec(
            event_name="CustomizeProduct",
            notes="Fired when a user customises a product.",
        ),
        # ── Donate ───────────────────────────────────────────────────────────
        EventSpec(
            event_name="Donate",
            notes="Fired when a user donates.",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Donation amount."),
            ),
        ),
        # ── FindLocation ─────────────────────────────────────────────────────
        EventSpec(
            event_name="FindLocation",
            notes="Fired when a user searches for a physical store location.",
        ),
        # ── Schedule ─────────────────────────────────────────────────────────
        EventSpec(
            event_name="Schedule",
            notes="Fired when a user schedules an appointment.",
        ),
        # ── StartTrial ───────────────────────────────────────────────────────
        EventSpec(
            event_name="StartTrial",
            notes="Fired when a user starts a free trial.",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Value of the trial subscription."),
                ParamSpec("predicted_ltv", "number", "Predicted lifetime value."),
            ),
        ),
        # ── Subscribe ────────────────────────────────────────────────────────
        EventSpec(
            event_name="Subscribe",
            notes="Fired when a user subscribes to a service.",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Subscription value."),
                ParamSpec("predicted_ltv", "number", "Predicted lifetime value."),
            ),
        ),
        # ── SubmitApplication ────────────────────────────────────────────────
        EventSpec(
            event_name="SubmitApplication",
            notes="Fired when a user submits an application (job, loan, etc.).",
        ),
        # ── ViewCategory ─────────────────────────────────────────────────────
        EventSpec(
            event_name="ViewCategory",
            notes="Non-standard but widely used: product category page view.",
            recommended_params=(
                ParamSpec("content_category", "string", "Category name."),
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Category value estimate."),
            ),
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="meta.pixel.test_event_code_not_in_prod",
            description="'test_event_code' must not be present in production pixel tags.",
            severity="critical",
            remediation="Remove the 'test_event_code' parameter from the GTM tag before deploying to production.",
            detection_hint="test_event_code",
            must_be_present=False,
        ),
        GlobalRule(
            rule_id="meta.pixel.pixel_id_not_placeholder",
            description="Meta Pixel ID must not be a placeholder value.",
            severity="critical",
            remediation="Replace the placeholder pixel ID with your actual Meta Pixel ID.",
            detection_hint="YOUR_PIXEL_ID",
            must_be_present=False,
        ),
    ),
)
