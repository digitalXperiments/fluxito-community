"""
Live Tag Test — Network Request Parser
=========================================

Decodes raw network request captures (as reported by Claude from browser
DevTools network tab) into structured platform + event + params dicts that
can be passed to the Rule Book validator.

Each platform sends data in a different format:
  GA4           POST /g/collect   — URL-encoded body  (en=, ep.*=, etc.)
  Meta Pixel    GET  /tr/         — query string       (e=, cd[event_name]=)
  TikTok        POST /api/v2/...  — JSON body
  Snap          POST /scevent...  — JSON body
  Pinterest     GET  /ct/...      — query string
  Twitter       GET  /...         — query string
  Microsoft UET GET  bat.bing.com — query string
  Amplitude     POST /...         — JSON body
  Segment       POST /...         — JSON body
  Generic       any               — best-effort JSON parse

Public API
----------
  parse_request(url, method, body) → ParsedRequest | None
  detect_platform_from_url(url)    → str | None
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ParsedRequest:
    platform: str
    event_name: str | None
    params: dict
    raw_url: str
    raw_body: str | None = None
    confidence: str = "medium"  # high | medium | low
    parse_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "event_name": self.event_name,
            "params": self.params,
            "raw_url": self.raw_url[:200] if self.raw_url else None,
            "confidence": self.confidence,
            "parse_errors": self.parse_errors,
        }


# ---------------------------------------------------------------------------
# Platform URL detection patterns
# ---------------------------------------------------------------------------

_URL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"google-analytics\.com/g/collect", re.I), "ga4"),
    (re.compile(r"analytics\.google\.com/g/collect", re.I), "ga4"),
    (re.compile(r"connect\.facebook\.net.*events", re.I), "facebook_pixel"),
    (re.compile(r"facebook\.com/tr", re.I), "facebook_pixel"),
    (re.compile(r"analytics\.tiktok\.com", re.I), "tiktok_pixel"),
    (re.compile(r"business-api\.tiktok\.com", re.I), "tiktok_pixel"),
    (re.compile(r"sc-static\.net/scevent", re.I), "snap_pixel"),
    (re.compile(r"tr\.snapchat\.com", re.I), "snap_pixel"),
    (re.compile(r"ct\.pinterest\.com", re.I), "pinterest_tag"),
    (re.compile(r"ads-twitter\.com", re.I), "twitter_pixel"),
    (re.compile(r"bat\.bing\.com", re.I), "microsoft_uet"),
    (re.compile(r"static\.criteo\.net", re.I), "criteo"),
    (re.compile(r"api\.amplitude\.com", re.I), "amplitude"),
    (re.compile(r"cdn\.amplitude\.com", re.I), "amplitude"),
    (re.compile(r"api\.segment\.io", re.I), "segment"),
    (re.compile(r"cdn\.segment\.com", re.I), "segment"),
    (re.compile(r"api\.mixpanel\.com", re.I), "mixpanel"),
    (re.compile(r"app\.posthog\.com|t\.posthog\.com", re.I), "posthog"),
    (re.compile(r"snap\.licdn\.com", re.I), "linkedin_insight"),
    (re.compile(r"googleadservices\.com", re.I), "google_ads_conversion"),
    (re.compile(r"doubleclick\.net", re.I), "floodlight"),
    (re.compile(r"demdex\.net|omtrdc\.net", re.I), "adobe_analytics"),
]


def detect_platform_from_url(url: str) -> str | None:
    """Return the platform slug for a network request URL, or None."""
    for pattern, platform in _URL_PATTERNS:
        if pattern.search(url):
            return platform
    return None


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


def parse_request(
    url: str,
    method: str = "GET",
    body: str | bytes | None = None,
) -> ParsedRequest | None:
    """
    Parse a network request into a structured ParsedRequest.

    ``url``    — full request URL (including query string for GET requests).
    ``method`` — HTTP method ("GET" or "POST").
    ``body``   — request body as string or bytes (for POST requests).

    Returns None if the URL doesn't match any known platform.
    """
    platform = detect_platform_from_url(url)
    if not platform:
        return None

    body_str: str | None = None
    if isinstance(body, bytes):
        try:
            body_str = body.decode("utf-8", errors="replace")
        except Exception:
            body_str = None
    elif isinstance(body, str):
        body_str = body

    try:
        if platform == "ga4":
            return _parse_ga4(url, method, body_str)
        if platform == "facebook_pixel":
            return _parse_meta_pixel(url, method, body_str)
        if platform == "tiktok_pixel":
            return _parse_tiktok(url, method, body_str)
        if platform == "snap_pixel":
            return _parse_json_body(url, platform, method, body_str)
        if platform == "amplitude":
            return _parse_amplitude(url, method, body_str)
        if platform == "segment":
            return _parse_segment(url, method, body_str)
        # Fallback: try query string then JSON body
        return _parse_generic(url, platform, method, body_str)
    except Exception as exc:
        logger.debug(f"parse_request failed for {platform}: {exc}")
        return ParsedRequest(
            platform=platform,
            event_name=None,
            params={},
            raw_url=url,
            raw_body=body_str,
            confidence="low",
            parse_errors=[str(exc)],
        )


# ---------------------------------------------------------------------------
# Platform-specific parsers
# ---------------------------------------------------------------------------


def _parse_ga4(url: str, method: str, body: str | None) -> ParsedRequest:
    """
    GA4 Measurement Protocol v2 (/g/collect).

    POST body is URL-encoded: en=<event_name>&ep.<param>=<value>&epn.<param>=<number>
    """
    params: dict = {}
    errors: list[str] = []

    source = body or ""
    if not source and "?" in url:
        source = url.split("?", 1)[1]

    # Decode URL-encoded pairs
    try:
        qs = urllib.parse.parse_qs(source, keep_blank_values=True)
    except Exception as e:
        errors.append(f"QS parse error: {e}")
        qs = {}

    # GA4 field mapping
    event_name: str | None = None
    for key, values in qs.items():
        val = values[0] if values else ""
        if key == "en":
            event_name = val
        elif key.startswith("ep."):
            params[key[3:]] = val
        elif key.startswith("epn."):
            # Numeric event parameter
            try:
                params[key[4:]] = float(val)
            except ValueError:
                params[key[4:]] = val
        elif key.startswith("pr1"):
            # Product param (ecommerce) — simplified
            sub_key = key[3:]
            params.setdefault("items", [{}])
            params["items"][0][sub_key] = val
        elif key in ("v", "tid", "cid", "sid", "sct", "seg", "_p"):
            # GA4 standard fields
            params[f"_ga4_{key}"] = val

    return ParsedRequest(
        platform="ga4",
        event_name=event_name,
        params=params,
        raw_url=url,
        raw_body=body,
        confidence="high",
        parse_errors=errors,
    )


def _parse_meta_pixel(url: str, method: str, body: str | None) -> ParsedRequest:
    """
    Meta Pixel — data typically in query string for GET /tr/ or POST body.
    """
    params: dict = {}
    errors: list[str] = []
    event_name: str | None = None

    # Combine URL query string and body
    sources = []
    if "?" in url:
        sources.append(url.split("?", 1)[1])
    if body:
        sources.append(body)

    for source in sources:
        try:
            qs = urllib.parse.parse_qs(source, keep_blank_values=True)
        except Exception as e:
            errors.append(str(e))
            continue
        for key, values in qs.items():
            val = values[0] if values else ""
            if key in ("e", "ev", "event"):
                event_name = val
            elif key.startswith("cd["):
                # Custom data: cd[event_name]=Purchase, cd[value]=99
                inner = key[3:-1] if key.endswith("]") else key[3:]
                if inner == "event_name":
                    event_name = val
                else:
                    params[inner] = _coerce_numeric(val)
            else:
                params[key] = _coerce_numeric(val)

    return ParsedRequest(
        platform="facebook_pixel",
        event_name=event_name,
        params=params,
        raw_url=url,
        raw_body=body,
        confidence="medium",
        parse_errors=errors,
    )


def _parse_tiktok(url: str, method: str, body: str | None) -> ParsedRequest:
    """TikTok Pixel — JSON body."""
    params: dict = {}
    event_name: str | None = None
    errors: list[str] = []

    if body:
        try:
            data = json.loads(body)
            # TikTok sends: {data: [{event: "Purchase", properties: {...}}]}
            events_list = data.get("data") or [data]
            if events_list:
                ev = events_list[0]
                event_name = ev.get("event")
                props = ev.get("properties") or {}
                params.update(props)
        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")
            # Try URL-encoded fallback
            try:
                qs = urllib.parse.parse_qs(body)
                event_name = (qs.get("event") or [None])[0]
                for k, v in qs.items():
                    params[k] = v[0] if v else ""
            except Exception:
                pass

    return ParsedRequest(
        platform="tiktok_pixel",
        event_name=event_name,
        params=params,
        raw_url=url,
        raw_body=body,
        confidence="medium",
        parse_errors=errors,
    )


def _parse_amplitude(url: str, method: str, body: str | None) -> ParsedRequest:
    """Amplitude HTTP API v2 — JSON body."""
    params: dict = {}
    event_name: str | None = None
    errors: list[str] = []

    if body:
        try:
            data = json.loads(body)
            events = data.get("events") or [data]
            if events:
                ev = events[0]
                event_name = ev.get("event_type") or ev.get("event")
                params.update(ev.get("event_properties") or {})
                user_props = ev.get("user_properties") or {}
                params.update({f"user.{k}": v for k, v in user_props.items()})
        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")

    return ParsedRequest(
        platform="amplitude",
        event_name=event_name,
        params=params,
        raw_url=url,
        raw_body=body,
        confidence="medium",
        parse_errors=errors,
    )


def _parse_segment(url: str, method: str, body: str | None) -> ParsedRequest:
    """Segment Analytics.js — JSON body."""
    params: dict = {}
    event_name: str | None = None
    errors: list[str] = []

    if body:
        try:
            data = json.loads(body)
            event_name = data.get("event") or data.get("name")
            params.update(data.get("properties") or {})
            params.update(data.get("traits") or {})
        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")

    return ParsedRequest(
        platform="segment",
        event_name=event_name,
        params=params,
        raw_url=url,
        raw_body=body,
        confidence="medium",
        parse_errors=errors,
    )


def _parse_json_body(url: str, platform: str, method: str, body: str | None) -> ParsedRequest:
    """Generic JSON body parser."""
    params: dict = {}
    event_name: str | None = None
    errors: list[str] = []

    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                event_name = data.get("event") or data.get("event_name") or data.get("ev")
                params = {k: v for k, v in data.items() if k not in ("event", "event_name", "ev")}
            elif isinstance(data, list) and data:
                first = data[0] if isinstance(data[0], dict) else {}
                event_name = first.get("event") or first.get("event_name")
                params = dict(first)
        except json.JSONDecodeError as e:
            errors.append(str(e))

    return ParsedRequest(
        platform=platform,
        event_name=event_name,
        params=params,
        raw_url=url,
        raw_body=body,
        confidence="low",
        parse_errors=errors,
    )


def _parse_generic(url: str, platform: str, method: str, body: str | None) -> ParsedRequest:
    """Try query string first, then JSON body."""
    params: dict = {}
    event_name: str | None = None
    errors: list[str] = []

    # Try URL query string
    if "?" in url:
        try:
            qs = urllib.parse.parse_qs(url.split("?", 1)[1])
            for k, v in qs.items():
                val = v[0] if v else ""
                if k in ("event", "e", "ev", "event_name"):
                    event_name = val
                else:
                    params[k] = _coerce_numeric(val)
        except Exception as e:
            errors.append(str(e))

    # Try JSON body
    if body and not params:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                event_name = event_name or data.get("event") or data.get("event_name")
                params.update({k: v for k, v in data.items() if k not in ("event", "event_name")})
        except Exception as e:
            errors.append(str(e))

    return ParsedRequest(
        platform=platform,
        event_name=event_name,
        params=params,
        raw_url=url,
        raw_body=body,
        confidence="low",
        parse_errors=errors,
    )


def _coerce_numeric(val: str) -> Any:
    """Try to convert a string value to int or float, return str if not possible."""
    try:
        if "." in val:
            return float(val)
        return int(val)
    except (ValueError, TypeError):
        return val
