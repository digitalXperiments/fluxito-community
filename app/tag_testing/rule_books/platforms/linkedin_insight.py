"""
Rule Book — LinkedIn Insight Tag
==================================
Spec version: LinkedIn/2024-09
Docs: https://www.linkedin.com/help/lms/answer/a424690
"""

from app.tag_testing.rule_books.base import EventSpec, GlobalRule, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="linkedin_insight",
    display_name="LinkedIn Insight Tag",
    spec_version="LinkedIn/2024-09",
    docs_url="https://www.linkedin.com/help/lms/answer/a424690",
    gtm_type_codes=(),
    detection_patterns=(
        r"snap\.licdn\.com",
        r"_linkedin_partner_id",
        r"lintrk\s*\(",
        r"linkedin.*insight",
    ),
    name_prefix_hints=("linkedin ", "li insight", "li "),
    events=(
        EventSpec(
            event_name="conversion",
            notes="LinkedIn conversion event (via lintrk API).",
            required_params=(ParamSpec("conversionId", "string", "LinkedIn conversion ID.", required=True),),
        ),
    ),
    global_rules=(
        GlobalRule(
            rule_id="linkedin.insight.partner_id_present",
            description="LinkedIn Partner ID (_linkedin_partner_id) must be set.",
            severity="critical",
            remediation="Set the _linkedin_partner_id variable to your LinkedIn Partner ID.",
            detection_hint="_linkedin_partner_id",
            must_be_present=True,
        ),
    ),
)
