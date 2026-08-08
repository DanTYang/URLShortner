"""Extract analytics facets from an API Gateway event.

No third-party GeoIP database is required. When the API is fronted by
CloudFront, AWS resolves the client IP at the edge and injects a
``CloudFront-Viewer-Country`` header — free, accurate, and the raw IP never
reaches this code.

``get_referrer`` deliberately keeps only the *host*. Full referrer URLs
routinely carry search terms, session tokens and internal paths; data that is
never collected cannot leak.
"""

from urllib.parse import urlparse


def _headers(event):
    """Return request headers with lowercased keys.

    HTTP header names are case-insensitive, and clients and proxies disagree
    about casing (``Referer``, ``referer``, ``REFERER``). Normalising once here
    lets every reader below index lowercase and be correct.
    """
    if event.get("headers") is None:
        return {}
    return {str(k).lower(): v for k, v in event.get("headers").items()}


def get_country(event):
    """Best-effort ISO-3166 country code, or ``"UNKNOWN"``.

    Priority: ``CloudFront-Viewer-Country``, then ``X-Country`` (an injection
    point for tests and local curl), then the sentinel.

    ``"UNKNOWN"`` is returned intact rather than truncated — ``"UN"`` is a real
    country code and reporting it would be a fabrication.
    """
    headers = _headers(event)
    country = headers.get("cloudfront-viewer-country") or headers.get("x-country")
    if country is None:
        return "UNKNOWN"
    return country.upper()[:2]


def get_referrer(event):
    """Return the referring host, or ``"direct"`` when there is none.

    Only ``netloc`` is kept: analytics group cleanly by traffic source, and no
    query strings or paths are retained. A malformed referrer yields an empty
    host and falls back to ``"direct"`` rather than producing an empty facet.
    """
    headers = _headers(event)
    # HTTP misspelled "referrer" as "referer" in 1996; some clients send the
    # correct spelling, so both are checked.
    header = headers.get("referer") or headers.get("referrer")
    if header is None:
        return "direct"
    netloc = urlparse(header).netloc
    if not netloc:
        return "direct"
    return netloc.lower()


def get_user_agent(event):
    """Return the User-Agent header, or an empty string."""
    return _headers(event).get("user-agent", "")


def get_source_ip(event):
    """Return the client IP as API Gateway reports it, or an empty string.

    Every level is guarded with ``or {}`` rather than a ``.get`` default: the
    keys exist with null values, and a default only applies when a key is
    absent.

    Provided for completeness — nothing in this service stores the result. An
    IP address is personal data under GDPR, and country already arrives from
    CloudFront without it.
    """
    return ((event.get("requestContext") or {}).get("identity") or {}).get(
        "sourceIp", ""
    )
