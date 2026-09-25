"""Rule Book — Mixpanel"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="mixpanel",
    display_name="Mixpanel",
    spec_version="Mixpanel/2024-09",
    docs_url="https://developer.mixpanel.com/docs/javascript",
    gtm_type_codes=(),
    detection_patterns=(r"cdn\.mxpnl\.com", r"mixpanel\.track\s*\(", r"mixpanel\.init\s*\("),
    name_prefix_hints=("mixpanel ",),
    events=(
        EventSpec(
            event_name="Purchase",
            notes="Revenue event — should use mixpanel.track with $amount.",
            required_params=(
                ParamSpec(
                    "$amount",
                    "number",
                    "Revenue amount (Mixpanel revenue property).",
                    required=True,
                    min_value=0,
                ),
            ),
            recommended_params=(
                ParamSpec("$currency", "string", "ISO 4217 currency code."),
                ParamSpec("order_id", "string", "Unique transaction ID."),
                ParamSpec("product_count", "integer", "Number of products."),
            ),
        ),
        EventSpec(event_name="Add to Cart", notes="Item added to cart."),
        EventSpec(event_name="View Product", notes="Product detail viewed."),
        EventSpec(event_name="Begin Checkout", notes="Checkout started."),
        EventSpec(event_name="Sign Up", notes="User signed up."),
        EventSpec(event_name="Log In", notes="User logged in."),
        EventSpec(event_name="Search", notes="Search performed."),
    ),
    global_rules=(
        GlobalRule(
            rule_id="mixpanel.project_token_present",
            description="Mixpanel project token must be present in the init call.",
            severity="critical",
            remediation="Initialize Mixpanel with your project token: mixpanel.init('YOUR_TOKEN').",
            detection_hint="mixpanel.init(",
            must_be_present=True,
        ),
    ),
)
