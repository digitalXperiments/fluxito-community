"""
Rule Book — Adobe Analytics
=============================
Spec version: AdobeAnalytics/2024-10
Docs: https://experienceleague.adobe.com/docs/analytics/implementation/vars/overview.html

Covers both legacy AppMeasurement (s.t() / s.tl()) and
Adobe Experience Platform (AEP) Web SDK (alloy.sendEvent) patterns.
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="adobe_analytics",
    display_name="Adobe Analytics",
    spec_version="AdobeAnalytics/2024-10",
    docs_url="https://experienceleague.adobe.com/docs/analytics/implementation/vars/overview.html",
    gtm_type_codes=(),
    detection_patterns=(
        r"adobe.*analytics",
        r"AppMeasurement",
        r"\bs\.t\s*\(",
        r"sc\.omtrdc\.net",
        r"alloy\.sendEvent",
    ),
    name_prefix_hints=("adobe analytics", "aaa ", "adobe "),
    events=(
        EventSpec(
            event_name="purchase",
            notes="Purchase / transaction event (s.events = 'purchase').",
            required_params=(
                ParamSpec(
                    "purchaseID", "string", "Unique purchase ID to prevent duplicate counting.", required=True
                ),
                ParamSpec("s.events", "string", "Must contain 'purchase'.", required=True),
                ParamSpec("s.products", "string", "Semicolon-delimited products string.", required=True),
                ParamSpec(
                    "s.currencyCode", "string", "ISO 4217 currency code.", required=True, regex=r"^[A-Z]{3}$"
                ),
            ),
            recommended_params=(
                ParamSpec("s.pageName", "string", "Page name."),
                ParamSpec("s.channel", "string", "Channel."),
            ),
        ),
        EventSpec(
            event_name="prodView",
            notes="Product detail view (s.events = 'prodView').",
            required_params=(
                ParamSpec("s.events", "string", "Must contain 'prodView'.", required=True),
                ParamSpec("s.products", "string", "Semicolon-delimited products string.", required=True),
            ),
        ),
        EventSpec(
            event_name="scAdd",
            notes="Add to cart (s.events = 'scAdd').",
            required_params=(
                ParamSpec("s.events", "string", "Must contain 'scAdd'.", required=True),
                ParamSpec("s.products", "string", "Semicolon-delimited products string.", required=True),
            ),
        ),
        EventSpec(
            event_name="scCheckout",
            notes="Checkout (s.events = 'scCheckout').",
            required_params=(
                ParamSpec("s.events", "string", "Must contain 'scCheckout'.", required=True),
                ParamSpec("s.products", "string", "Semicolon-delimited products string.", required=True),
            ),
        ),
        EventSpec(
            event_name="scView",
            notes="Cart view (s.events = 'scView').",
            required_params=(ParamSpec("s.events", "string", "Must contain 'scView'.", required=True),),
        ),
        EventSpec(
            event_name="event1",
            notes="Custom success event (eVar/event based implementation).",
            recommended_params=(ParamSpec("s.eVar1", "string", "Conversion variable."),),
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="adobe.report_suite_id_present",
            description="Adobe Analytics Report Suite ID (s.account) must be present.",
            severity="critical",
            remediation="Set s.account = 'your-report-suite-id' in the Adobe Analytics tag.",
            detection_hint="s.account",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="adobe.tracking_server_present",
            description="Adobe Analytics tracking server (s.trackingServer) must be configured.",
            severity="critical",
            remediation="Set s.trackingServer to your Adobe Analytics tracking server URL.",
            detection_hint="s.trackingServer",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="adobe.no_debug_in_prod",
            description="Adobe Analytics debug mode (s.debugTracking) must not be enabled in production.",
            severity="warning",
            remediation="Remove s.debugTracking = true from production tag configuration.",
            detection_hint="debugTracking",
            must_be_present=False,
        ),
    ),
)
