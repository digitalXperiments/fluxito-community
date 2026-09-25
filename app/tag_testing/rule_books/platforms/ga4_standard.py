"""
Rule Book — Google Analytics 4 (Standard / Non-ecommerce Events)
=================================================================
Spec version: GA4/2024-11
Docs: https://developers.google.com/analytics/devguides/collection/ga4/reference/events

Covers standard GA4 events for engagement, lead generation, content, and
user lifecycle that do not belong to the ecommerce taxonomy.
"""

from app.tag_testing.rule_books.base import EventSpec, ParamSpec, RuleBook

RULE_BOOK = RuleBook(
    platform="ga4_standard",
    display_name="Google Analytics 4 (Standard Events)",
    spec_version="GA4/2024-11",
    docs_url="https://developers.google.com/analytics/devguides/collection/ga4/reference/events",
    gtm_type_codes=("gaawc", "gaawe", "googtag"),
    detection_patterns=(r"gtag\s*\(\s*['\"]event['\"]",),
    name_prefix_hints=(),  # shared with ga4_ecom — identified by ga4 rule book
    events=(
        EventSpec(
            event_name="generate_lead",
            notes="Fired when a lead is generated (e.g. form submission).",
            recommended_params=(
                ParamSpec("currency", "string", "ISO 4217 currency code."),
                ParamSpec("value", "number", "Estimated value of the lead."),
            ),
        ),
        EventSpec(
            event_name="sign_up",
            notes="Fired when a user signs up.",
            recommended_params=(
                ParamSpec("method", "string", "The method used to sign up (e.g. email, Google)."),
            ),
        ),
        EventSpec(
            event_name="login",
            notes="Fired when a user logs in.",
            recommended_params=(ParamSpec("method", "string", "The method used to log in."),),
        ),
        EventSpec(
            event_name="search",
            notes="Fired when a user performs a search.",
            required_params=(
                ParamSpec("search_term", "string", "The term that was searched for.", required=True),
            ),
        ),
        EventSpec(
            event_name="share",
            notes="Fired when a user shares content.",
            recommended_params=(
                ParamSpec("method", "string", "Share method (e.g. twitter, email)."),
                ParamSpec("content_type", "string", "Type of content being shared."),
                ParamSpec("item_id", "string", "ID of the shared item."),
            ),
        ),
        EventSpec(
            event_name="tutorial_begin",
            notes="Fired when a user begins a tutorial.",
        ),
        EventSpec(
            event_name="tutorial_complete",
            notes="Fired when a user completes a tutorial.",
        ),
        EventSpec(
            event_name="level_up",
            notes="Fired when a user levels up in a game.",
            required_params=(
                ParamSpec("level", "integer", "The level number the user leveled up to.", required=True),
                ParamSpec("character", "string", "The character used."),
            ),
        ),
        EventSpec(
            event_name="unlock_achievement",
            notes="Fired when a user unlocks an achievement.",
            required_params=(
                ParamSpec("achievement_id", "string", "The ID of the achievement.", required=True),
            ),
        ),
        EventSpec(
            event_name="page_view",
            notes="Fired on each page view (automatically collected by GA4).",
            recommended_params=(
                ParamSpec("page_title", "string", "Page title."),
                ParamSpec("page_location", "string", "Full URL of the page."),
                ParamSpec("page_referrer", "string", "Referrer URL."),
            ),
        ),
        EventSpec(
            event_name="scroll",
            notes="Fired when a user scrolls to 90% of the page (auto-collected).",
            recommended_params=(ParamSpec("percent_scrolled", "integer", "Percent of page scrolled (90)."),),
        ),
        EventSpec(
            event_name="file_download",
            notes="Fired when a link to a file is clicked (auto-collected).",
            recommended_params=(
                ParamSpec("file_name", "string", "Filename downloaded."),
                ParamSpec("file_extension", "string", "File extension."),
                ParamSpec("link_url", "string", "URL of the download link."),
            ),
        ),
        EventSpec(
            event_name="video_start",
            notes="Fired when a video starts playing (auto-collected for embedded YouTube).",
            recommended_params=(
                ParamSpec("video_title", "string", "Video title."),
                ParamSpec("video_url", "string", "Video URL."),
                ParamSpec("video_provider", "string", "Video provider (e.g. youtube)."),
            ),
        ),
        EventSpec(
            event_name="video_progress",
            notes="Fired when a video progresses (auto-collected).",
            recommended_params=(
                ParamSpec("video_title", "string", "Video title."),
                ParamSpec("video_percent", "integer", "Progress percentage."),
            ),
        ),
        EventSpec(
            event_name="video_complete",
            notes="Fired when a video completes (auto-collected).",
            recommended_params=(
                ParamSpec("video_title", "string", "Video title."),
                ParamSpec("video_url", "string", "Video URL."),
            ),
        ),
        EventSpec(
            event_name="click",
            notes="Fired when a link is clicked (auto-collected outbound clicks).",
            recommended_params=(
                ParamSpec("link_url", "string", "URL of the clicked link."),
                ParamSpec("link_text", "string", "Anchor text."),
                ParamSpec("link_domain", "string", "Domain of the clicked link."),
                ParamSpec("outbound", "boolean", "Whether the link is outbound."),
            ),
        ),
        EventSpec(
            event_name="exception",
            notes="Fired when an app or site error occurs.",
            recommended_params=(
                ParamSpec("description", "string", "Description of the error."),
                ParamSpec("fatal", "boolean", "Whether the error was fatal."),
            ),
        ),
    ),
    global_rules=(),
)
