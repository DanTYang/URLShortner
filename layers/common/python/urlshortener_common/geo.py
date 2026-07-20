"""Extract analytics facets (country, referrer, client) from an API event.

None of this requires a third-party GeoIP database. When the API sits behind
CloudFront, AWS injects a ``CloudFront-Viewer-Country`` header derived from the
client IP — a free, accurate ISO country code. We read that when present and
degrade gracefully to ``UNKNOWN`` otherwise.
"""

from typing import Any
from urllib.parse import urlparse


def _headers(event: dict[str, Any]) -> dict[str, str]:
    """Return request headers with case-insensitive lookup."""
    raw = event.get("headers") or {}
    return {str(k).lower(): v for k, v in raw.items()}


def get_country(event: dict[str, Any]) -> str:
    """Best-effort ISO-3166 country code for the requester.

    Priority:
      1. ``CloudFront-Viewer-Country`` (present when fronted by CloudFront).
      2. ``X-Country`` (lets callers/tests inject a value).
      3. ``"UNKNOWN"``.
    """
    headers = _headers(event)
    country = (
        headers.get("cloudfront-viewer-country")
        or headers.get("x-country")
        or "UNKNOWN"
    )
    return country.upper()[:2] if country != "UNKNOWN" else "UNKNOWN"


def get_referrer(event: dict[str, Any]) -> str:
    """Return the referring host, or ``"direct"`` if there is none.

    We deliberately keep only the *host* (e.g. ``t.co``), not the full URL, so
    analytics group cleanly by traffic source and we don't retain query
    strings that may contain personal data.
    """
    headers = _headers(event)
    # HTTP misspelled "referrer" as "referer" in 1996 and we're stuck with it.
    referer = headers.get("referer") or headers.get("referrer")
    if not referer:
        return "direct"
    host = urlparse(referer).netloc
    return host.lower() or "direct"


def get_user_agent(event: dict[str, Any]) -> str:
    """Return the User-Agent string, or empty."""
    return _headers(event).get("user-agent", "")


def get_source_ip(event: dict[str, Any]) -> str:
    """Return the client IP as reported by API Gateway (best effort)."""
    ctx = event.get("requestContext") or {}
    identity = ctx.get("identity") or {}
    return identity.get("sourceIp", "")
