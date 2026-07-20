"""POST /urls — create a short link.

Request body (JSON)::

    {
      "url": "https://example.com/some/very/long/path",  # required
      "customCode": "spring-sale",                        # optional
      "expiresInDays": 30,                                # optional
      "owner": "user-123"                                 # optional
    }

Behaviour
---------
* If ``customCode`` is supplied it is validated and claimed atomically; a clash
  returns ``409 Conflict``.
* Otherwise a random base62 code is generated and claimed, retrying on
  collision (see :func:`urlshortener_common.shortcode.generate_unique_code`).
* ``expiresInDays`` sets an absolute expiry; the link then 410s after that time
  and is eventually reaped by DynamoDB TTL.
"""

import time
from urllib.parse import urlparse

from urlshortener_common import config, dynamo, metrics, responses, shortcode

# Cap how far in the future a link may be set to expire (~10 years).
_MAX_EXPIRY_DAYS = 3650


def _is_valid_url(url: str) -> bool:
    """Accept only absolute http(s) URLs with a host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _compute_expiry(body: dict) -> int | None:
    """Translate ``expiresInDays`` into an absolute epoch, or ``None``.

    Raises
    ------
    ValueError
        If the value is not a positive number within the allowed range.
    """
    days = body.get("expiresInDays")
    if days is None:
        return None
    if not isinstance(days, (int, float)) or days <= 0 or days > _MAX_EXPIRY_DAYS:
        raise ValueError(
            f"expiresInDays must be a positive number <= {_MAX_EXPIRY_DAYS}."
        )
    return int(time.time()) + int(days * 86400)


def handler(event, context):
    # ---- Parse & validate input ------------------------------------------
    try:
        body = responses.parse_json_body(event)
    except ValueError:
        return responses.error(400, "Request body must be valid JSON.")

    long_url = (body.get("url") or "").strip()
    if not long_url:
        return responses.error(400, "Field 'url' is required.")
    if not _is_valid_url(long_url):
        return responses.error(400, "Field 'url' must be an absolute http(s) URL.")

    try:
        expires_at = _compute_expiry(body)
    except ValueError as exc:
        return responses.error(400, str(exc))

    owner = (body.get("owner") or "anonymous").strip() or "anonymous"

    # ---- Claim a short code ----------------------------------------------
    custom_code = body.get("customCode")
    if custom_code:
        custom_code = custom_code.strip()
        if not shortcode.is_valid_custom_code(custom_code):
            return responses.error(
                400,
                "customCode must be 3-64 chars of letters, digits, '-' or '_' "
                "and not a reserved word.",
            )
        claimed = dynamo.put_url(
            custom_code,
            long_url,
            owner=owner,
            expires_at=expires_at,
            custom=True,
        )
        if not claimed:
            metrics.count("ShortCodeCollision")
            return responses.error(409, f"Short code '{custom_code}' is already taken.")
        code = custom_code
    else:
        # Track collisions for observability, then delegate retry logic to the
        # generator. The lambda passed to it performs the atomic claim.
        def try_claim(candidate: str) -> bool:
            ok = dynamo.put_url(
                candidate,
                long_url,
                owner=owner,
                expires_at=expires_at,
                custom=False,
            )
            if not ok:
                metrics.count("ShortCodeCollision")
            return ok

        try:
            code = shortcode.generate_unique_code(try_claim)
        except shortcode.ShortCodeCollisionError:
            return responses.error(
                503, "Could not allocate a unique short code; please retry."
            )

    # ---- Emit metrics & respond ------------------------------------------
    metrics.count("LinksCreated")

    base_url = _base_url(event)
    return responses.json_response(
        201,
        {
            "shortCode": code,
            "shortUrl": f"{base_url}/{code}",
            "longUrl": long_url,
            "owner": owner,
            "custom": bool(custom_code),
            "expiresAt": expires_at,
            "createdAt": dynamo.iso_now(),
        },
    )


def _base_url(event) -> str:
    """Reconstruct the public base URL of the API from the request context."""
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    host = headers.get("host", "")
    proto = headers.get("x-forwarded-proto", "https")
    ctx = event.get("requestContext") or {}
    stage = ctx.get("stage", "")
    if host and stage:
        return f"{proto}://{host}/{stage}"
    return f"https://{host}" if host else ""
