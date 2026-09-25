"""
Rule Book — Google Tag Manager Container Best Practices
=========================================================
Spec version: GTM/2024-11
Docs: https://support.google.com/tagmanager/answer/6103696

Container-level health and best-practice rules.  No per-event specs —
these are global structural rules applied to the container as a whole.
"""

from app.tag_testing.rule_books.base import GlobalRule, RuleBook

RULE_BOOK = RuleBook(
    platform="gtm_spec",
    display_name="Google Tag Manager (Container Best Practices)",
    spec_version="GTM/2024-11",
    docs_url="https://support.google.com/tagmanager/answer/6103696",
    gtm_type_codes=(),
    detection_patterns=(),
    name_prefix_hints=(),
    events=(),
    global_rules=(
        GlobalRule(
            rule_id="gtm.no_paused_tags",
            description="Paused tags exist in the container — review whether they should be removed or activated.",
            severity="warning",
            remediation="Remove or reactivate paused tags. Paused tags are a sign of incomplete implementations.",
            detection_hint="paused",
            must_be_present=False,
        ),
        GlobalRule(
            rule_id="gtm.no_firing_all_pages_blanket",
            description="Tags with an 'All Pages' trigger and no exclusion filters may fire on unintended pages.",
            severity="info",
            remediation="Review 'All Pages' triggers; add page path filters where appropriate.",
            detection_hint="",
            must_be_present=False,
        ),
        GlobalRule(
            rule_id="gtm.consent_mode_configured",
            description="Consent Mode must be configured for GDPR / CCPA compliance.",
            severity="warning",
            remediation=(
                "Add the Consent Initialization trigger and configure Consent Mode v2 "
                "with ad_storage and analytics_storage signals."
            ),
            detection_hint="consent",
            must_be_present=True,
        ),
        GlobalRule(
            rule_id="gtm.no_excessive_tags",
            description="Containers with more than 100 active tags may have performance impacts.",
            severity="info",
            remediation="Review and archive unused tags. Consider tag consolidation.",
            detection_hint="",
            must_be_present=False,
        ),
    ),
)
