"""POST /urls — create a short link.

Request body
------------
    {
      "url": "https://example.com/some/very/long/path",   # required
      "customCode": "spring-sale",                        # optional
      "expiresInDays": 30,                                # optional
      "owner": "user-123"                                 # optional
    }

Responses
---------
201  created
400  invalid input
409  the requested customCode is taken
503  no unique random code could be allocated within the retry budget

Handlers stay thin: this module owns HTTP concerns — parsing, validation,
status codes, response shape. Storage logic lives in ``dynamo``, short-code
logic in ``shortcode``.
"""

import time
from urllib.parse import urlparse

from urlshortener_common import dynamo, metrics, responses, shortcode

# Cap how far ahead a link may be set to expire (~10 years).
_MAX_EXPIRY_DAYS = 3650

# Longest destination URL accepted. Browsers stop being reliable well before
# 2 KB, and unbounded input is a liability: a DynamoDB item caps at 400 KB, so
# a large enough URL turns a clean rejection into an unhandled write failure —
# and storage is billed by the byte with no authentication in front.
_MAX_URL_LENGTH = 2048


def _is_valid_url(url):
    """True if ``url`` is an absolute http(s) URL within the length cap.

    The scheme check is an allowlist, not a blocklist. Without it a
    ``javascript:`` or ``data:`` URL could be shortened, and the short domain
    would lend credibility to an XSS payload. A blocklist is only ever a list
    of the attacks already thought of.
    """
    if len(url) > _MAX_URL_LENGTH:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    # Both halves matter: urlparse is forgiving and turns "not-a-url" into a
    # bare path with no scheme and no netloc rather than raising.
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _compute_expiry(body):
    """Translate ``expiresInDays`` into an absolute epoch, or None.

    Storing a relative duration would be meaningless on read, so the value is
    converted to the instant it expires.

    Raises
    ------
    ValueError
        If the value is not a positive number within the allowed range.
    """
    days = body.get("expiresInDays")
    if days is None:
        return None
    # bool is a subclass of int in Python, so `isinstance(True, int)` is True.
    # Excluded explicitly, or {"expiresInDays": true} silently means "1 day".
    if (
        isinstance(days, bool)
        or not isinstance(days, (int, float))
        or days <= 0
        or days > _MAX_EXPIRY_DAYS
    ):
        raise ValueError(
            f"expiresInDays must be a positive number no more than {_MAX_EXPIRY_DAYS}"
        )
    return int(time.time()) + int(days * 86400)


def handler(event, context):
    """Create a short link with an optional custom code and expiry.

    Each try/except below wraps exactly the call that can raise. A wider block
    would report an internal failure as a 400 — blaming the caller for our bug
    and swallowing the traceback that would have located it.
    """
    try:
        body = responses.parse_json_body(event)
    except ValueError:
        return responses.error(400, "Request body is not valid JSON")

    long_url = (body.get("url") or "").strip()
    if not long_url or not _is_valid_url(long_url):
        return responses.error(400, "Invalid URL")

    try:
        expires_at = _compute_expiry(body)
    except ValueError as exc:
        return responses.error(400, str(exc))

    owner = (body.get("owner") or "anonymous").strip() or "anonymous"
    custom_code = (body.get("customCode") or "").strip()

    if custom_code:
        if not shortcode.is_valid_custom_code(custom_code):
            return responses.error(400, "Invalid custom code")
        ok = dynamo.put_url(
            custom_code, long_url, owner=owner, expires_at=expires_at, custom=True
        )
        if not ok:
            metrics.count("ShortCodeCollision")
            # Terminal, not retryable: the caller asked for one specific code,
            # and silently substituting a different one would be worse.
            return responses.error(409, "Custom code already taken")
        code = custom_code
    else:

        def try_claim(candidate):
            """Attempt to atomically claim ``candidate``; True if it stuck."""
            ok = dynamo.put_url(
                candidate, long_url, owner=owner, expires_at=expires_at, custom=False
            )
            if not ok:
                metrics.count("ShortCodeCollision")
            return ok

        try:
            code = shortcode.generate_unique_code(try_claim)
        except shortcode.ShortCodeCollisionError:
            # 503, not 500: a retry genuinely will succeed, because the next
            # candidate is drawn fresh.
            return responses.error(503, "Could not find a free short code")

    metrics.count("LinksCreated")
    return responses.json_response(
        201,
        {
            "shortCode": code,
            "shortUrl": f"{_base_url(event)}/{code}",
            "longUrl": long_url,
            "owner": owner,
            "custom": bool(custom_code),
            "expiresAt": expires_at,
            "createdAt": dynamo.iso_now(),
        },
    )


def _base_url(event):
    """Reconstruct the API's public base URL from the request itself.

    Derived rather than configured, so dev, staging, prod and a local
    ``sam local`` on :3000 are all correct with no environment-specific
    settings to drift.
    """
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    host = headers.get("host", "")
    proto = headers.get("x-forwarded-proto", "https")
    stage = (event.get("requestContext") or {}).get("stage", "")
    if host and stage:
        return f"{proto}://{host}/{stage}"
    if host:
        return f"https://{host}"
    return ""
