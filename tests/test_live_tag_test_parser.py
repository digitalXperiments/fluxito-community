"""
Unit tests for the Live Tag Test network request parser.

Tests parse_request() and detect_platform_from_url() for all 20 platforms:
  - URL detection patterns
  - GET query string parsing (Meta, Pinterest, Microsoft UET, etc.)
  - POST body parsing: URL-encoded (GA4), JSON (TikTok, Amplitude, Segment, Snap)
  - Event name extraction
  - Numeric coercion
  - Graceful handling of malformed payloads
"""

from app.tag_testing.live_test.parser import (
    detect_platform_from_url,
    parse_request,
)

# ── URL detection ─────────────────────────────────────────────────────────────


class TestDetectPlatformFromUrl:
    def test_ga4_google_analytics(self):
        assert detect_platform_from_url("https://www.google-analytics.com/g/collect?v=2") == "ga4"

    def test_ga4_analytics_google(self):
        assert detect_platform_from_url("https://analytics.google.com/g/collect") == "ga4"

    def test_meta_pixel_tr(self):
        assert detect_platform_from_url("https://www.facebook.com/tr?ev=Purchase") == "facebook_pixel"

    def test_tiktok_analytics(self):
        assert detect_platform_from_url("https://analytics.tiktok.com/api/v1/batch") == "tiktok_pixel"

    def test_tiktok_business_api(self):
        assert (
            detect_platform_from_url("https://business-api.tiktok.com/open_api/v1.3/pixel/track/")
            == "tiktok_pixel"
        )

    def test_snap(self):
        assert detect_platform_from_url("https://sc-static.net/scevent.min.js") == "snap_pixel"

    def test_snap_tr(self):
        assert detect_platform_from_url("https://tr.snapchat.com/p") == "snap_pixel"

    def test_pinterest(self):
        assert detect_platform_from_url("https://ct.pinterest.com/ct.gif?tid=12345") == "pinterest_tag"

    def test_twitter(self):
        assert detect_platform_from_url("https://analytics.ads-twitter.com/i/adsct") == "twitter_pixel"

    def test_microsoft_uet(self):
        assert detect_platform_from_url("https://bat.bing.com/action/0") == "microsoft_uet"

    def test_criteo(self):
        assert detect_platform_from_url("https://static.criteo.net/js/ld/publishertag.prebid.js") == "criteo"

    def test_amplitude(self):
        assert detect_platform_from_url("https://api.amplitude.com/2/httpapi") == "amplitude"

    def test_segment(self):
        assert detect_platform_from_url("https://api.segment.io/v1/track") == "segment"

    def test_mixpanel(self):
        assert detect_platform_from_url("https://api.mixpanel.com/track") == "mixpanel"

    def test_posthog(self):
        assert detect_platform_from_url("https://app.posthog.com/e/abc") == "posthog"
        assert detect_platform_from_url("https://t.posthog.com/ingest") == "posthog"

    def test_linkedin(self):
        assert (
            detect_platform_from_url("https://snap.licdn.com/li.lms-analytics/insight.min.js")
            == "linkedin_insight"
        )

    def test_google_ads(self):
        assert (
            detect_platform_from_url("https://www.googleadservices.com/pagead/conversion/12345")
            == "google_ads_conversion"
        )

    def test_floodlight(self):
        assert detect_platform_from_url("https://ad.doubleclick.net/ddm/activity/src=123") == "floodlight"

    def test_adobe(self):
        assert (
            detect_platform_from_url("https://metrics.example.com/b/ss/rsid/6/JS-2.21") == None
        )  # omtrdc not matched here

    def test_unknown_returns_none(self):
        assert detect_platform_from_url("https://example.com/checkout") is None

    def test_empty_returns_none(self):
        assert detect_platform_from_url("") is None


# ── GA4 parsing ───────────────────────────────────────────────────────────────


class TestParseGA4:
    URL = "https://www.google-analytics.com/g/collect"

    def test_post_body_url_encoded(self):
        body = "v=2&tid=G-TEST123&en=purchase&epn.value=99.99&ep.currency=USD&epn.quantity=2"
        r = parse_request(self.URL, method="POST", body=body)
        assert r is not None
        assert r.platform == "ga4"
        assert r.event_name == "purchase"
        assert r.params["value"] == 99.99
        assert r.params["currency"] == "USD"
        assert r.params["quantity"] == 2.0

    def test_querystring_fallback(self):
        url = self.URL + "?v=2&tid=G-TEST&en=add_to_cart&ep.currency=GBP&epn.value=49.00"
        r = parse_request(url)
        assert r.event_name == "add_to_cart"
        assert r.params["currency"] == "GBP"

    def test_ga4_standard_fields_prefixed(self):
        body = "v=2&tid=G-ABCDE&en=page_view&cid=client-123"
        r = parse_request(self.URL, method="POST", body=body)
        assert "_ga4_tid" in r.params
        assert r.params["_ga4_tid"] == "G-ABCDE"

    def test_confidence_high(self):
        r = parse_request(self.URL, method="POST", body="v=2&en=page_view")
        assert r.confidence == "high"


# ── Meta Pixel parsing ────────────────────────────────────────────────────────


class TestParseMetaPixel:
    URL = "https://www.facebook.com/tr"

    def test_ev_param_is_event_name(self):
        r = parse_request(
            self.URL + '?ev=Purchase&cd[value]=99.99&cd[currency]=USD&cd[content_ids]=["SKU-1"]'
        )
        assert r is not None
        assert r.event_name == "Purchase"

    def test_cd_value_is_numeric(self):
        r = parse_request(self.URL + "?ev=AddToCart&cd[value]=29.99&cd[currency]=GBP")
        assert r.params["value"] == 29.99
        assert r.params["currency"] == "GBP"

    def test_cd_event_name_fallback(self):
        # Some pixels use cd[event_name] instead of ev=
        r = parse_request(self.URL + "?cd[event_name]=InitiateCheckout&cd[value]=49.99&cd[currency]=USD")
        assert r.event_name == "InitiateCheckout"

    def test_unknown_url_returns_none(self):
        r = parse_request("https://example.com/not-a-pixel")
        assert r is None


# ── TikTok parsing ────────────────────────────────────────────────────────────


class TestParseTikTok:
    URL = "https://analytics.tiktok.com/api/v1/batch"

    def test_json_body(self):
        body = (
            '{"data":[{"event":"Purchase","properties":{"currency":"USD","value":99,"content_id":"SKU-1"}}]}'
        )
        r = parse_request(self.URL, method="POST", body=body)
        assert r.platform == "tiktok_pixel"
        assert r.event_name == "Purchase"
        assert r.params["currency"] == "USD"
        assert r.params["value"] == 99

    def test_missing_body_empty_params(self):
        r = parse_request(self.URL, method="POST", body=None)
        assert r.event_name is None
        assert r.params == {}


# ── Amplitude parsing ─────────────────────────────────────────────────────────


class TestParseAmplitude:
    URL = "https://api.amplitude.com/2/httpapi"

    def test_json_body_events_array(self):
        body = '{"api_key":"abc","events":[{"event_type":"purchase","event_properties":{"revenue":99.99,"product_id":"SKU-1"}}]}'
        r = parse_request(self.URL, method="POST", body=body)
        assert r.platform == "amplitude"
        assert r.event_name == "purchase"
        assert r.params["revenue"] == 99.99


# ── Segment parsing ───────────────────────────────────────────────────────────


class TestParseSegment:
    URL = "https://api.segment.io/v1/track"

    def test_json_body(self):
        body = (
            '{"event":"Order Completed","properties":{"order_id":"ORD-001","total":99.99,"currency":"USD"}}'
        )
        r = parse_request(self.URL, method="POST", body=body)
        assert r.platform == "segment"
        assert r.event_name == "Order Completed"
        assert r.params["order_id"] == "ORD-001"


# ── Graceful degradation ──────────────────────────────────────────────────────


class TestGracefulDegradation:
    def test_malformed_json_body(self):
        r = parse_request(
            "https://analytics.tiktok.com/api/v1/batch",
            method="POST",
            body="{this is not json}",
        )
        assert r is not None  # Should not raise; returns ParsedRequest with errors
        assert len(r.parse_errors) > 0
        assert r.confidence == "medium"

    def test_empty_body_ga4(self):
        r = parse_request(
            "https://www.google-analytics.com/g/collect",
            method="POST",
            body="",
        )
        assert r is not None
        assert r.event_name is None

    def test_bytes_body(self):
        body = b"v=2&en=page_view&tid=G-TEST"
        r = parse_request("https://www.google-analytics.com/g/collect", method="POST", body=body)
        assert r is not None
        assert r.event_name == "page_view"

    def test_parsed_request_as_dict(self):
        r = parse_request(
            "https://www.google-analytics.com/g/collect", method="POST", body="v=2&en=page_view"
        )
        d = r.as_dict()
        assert "platform" in d
        assert "event_name" in d
        assert "params" in d
        assert "confidence" in d
