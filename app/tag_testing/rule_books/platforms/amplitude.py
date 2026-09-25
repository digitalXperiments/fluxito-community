"""
Rule Book — Amplitude
======================
Spec version: Amplitude/2024-10
Docs: https://www.docs.developers.amplitude.com/analytics/apis/http-v2-api/
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="amplitude",
    display_name="Amplitude Analytics",
    spec_version="Amplitude/2024-10",
    docs_url="https://www.docs.developers.amplitude.com/analytics/apis/http-v2-api/",
    gtm_type_codes=(),
    detection_patterns=(r"cdn\.amplitude\.com", r"amplitude\.getInstance\s*\(", r"amplitudeJS"),
    name_prefix_hints=("amplitude ",),
    events=(
        EventSpec(
            event_name="Revenue",
            notes="Amplitude revenue event (logRevenue).",
            required_params=(
                ParamSpec("productId", "string", "Product identifier.", required=True),
                ParamSpec("quantity", "integer", "Quantity purchased.", required=True),
                ParamSpec("price", "number", "Unit price.", required=True, min_value=0),
            ),
            recommended_params=(
                ParamSpec("revenueType", "string", "Revenue type (e.g. purchase, subscription)."),
                ParamSpec("receipt", "string", "Receipt for validation."),
            ),
        ),
        EventSpec(
            event_name="Add Item to Cart",
            notes="Item added to cart.",
            required_params=(
                ParamSpec("product_id", "string", "Product ID.", required=True),
                ParamSpec("price", "number", "Item price.", required=True, min_value=0),
                ParamSpec("quantity", "integer", "Quantity.", required=True),
            ),
        ),
        EventSpec(
            event_name="Start Checkout",
            notes="Checkout initiated.",
            recommended_params=(
                ParamSpec("total", "number", "Total value."),
                ParamSpec("currency", "string", "ISO 4217 code."),
            ),
        ),
        EventSpec(event_name="Complete Purchase", notes="Purchase completed."),
        EventSpec(event_name="View Product", notes="Product viewed."),
        EventSpec(event_name="Sign Up", notes="User signed up."),
        EventSpec(event_name="Log In", notes="User logged in."),
        EventSpec(event_name="Search", notes="Search performed."),
    ),
    global_rules=(
        GlobalRule(
            rule_id="amplitude.api_key_present",
            description="Amplitude API key must be present in the SDK initialization.",
            severity="critical",
            remediation="Initialize Amplitude with: amplitude.getInstance().init('YOUR_API_KEY').",
            detection_hint="amplitude.getInstance().init(",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="amplitude.no_debug_in_prod",
            description="Amplitude debug mode (logLevel: DEBUG) must not be enabled in production.",
            severity="warning",
            remediation="Remove or set logLevel to 'DISABLE' or 'ERROR' in production.",
            detection_hint="logLevel.*DEBUG",
            must_be_present=False,
        ),
    ),
)
