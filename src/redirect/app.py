"""GET /{shortCode} — resolve a short code and redirect to its target.

This is the hot path: redirects outnumber creates by orders of magnitude in any
real shortener, so everything here runs at production volume.

    1. one GetItem to resolve the code
    2. 404 if unknown, 410 if expired
    3. record the click (best effort)
    4. 302 with a short, bounded Cache-Control window

Analytics is deliberately best-effort. Resolving the link is the user's goal;
recording the click is ours. If the write fails we serve the redirect anyway
and lose the event, because a missing analytics row is strictly better than a
broken link.

Known limitation: the click write is synchronous, which triples the DynamoDB
operations on the busiest endpoint in the system. At high volume it belongs off
the request path entirely — DynamoDB Streams or an SQS fan-out.
"""

import time

from urlshortener_common import dynamo, geo, metrics, responses

# How long a client may cache a redirect. This is a correctness budget, not a
# performance setting: a cached response cannot be revoked, so this is the
# worst case for how long a deleted or expired link keeps resolving.
#
# Browser caches are per-client, so they do nothing for distinct visitors — a
# thousand people clicking a viral link is a thousand cache misses regardless.
# The cache only absorbs repeat clicks from the same browser, which makes a
# short window cheap: at a million redirects a month the difference between 60
# and 300 seconds is well under a dollar. A CDN in front would change that
# calculus, because a shared cache serves everyone from one fetch.
_MAX_CACHE_SECONDS = 60


def handler(event, context):
    """Resolve ``shortCode`` and issue a 302 to its target URL.

    Returns 400 if the code is missing, 404 if unknown, 410 if expired.

    404 versus 410 is deliberate: 410 Gone tells crawlers the resource existed
    and was removed, which de-indexes faster than a bare 404.

    302 rather than 301 because a cached redirect cannot be revoked and every
    link here can be deleted or expire. The cache window is capped at the link's
    remaining lifetime and never exceeds ``_MAX_CACHE_SECONDS``.
    """
    # pathParameters is None, not {}, when API Gateway has none to send.
    short_code = (event.get("pathParameters") or {}).get("shortCode", "")
    if not short_code:
        return responses.error(400, "Missing short code.")

    row = dynamo.get_url(short_code)
    if row is None:
        metrics.count("RedirectNotFound")
        return responses.error(404, "Short link not found.")

    # Enforced on read because DynamoDB's TTL sweeper is eventual — the row can
    # still be present hours after it lapsed.
    if dynamo.is_expired(row):
        metrics.count("RedirectExpired")
        return responses.error(410, "This short link has expired.")

    # Ordered after both checks: a 404 or a 410 is not a visit.
    _record_analytics(event, short_code)

    metrics.count("Redirects")
    return responses.redirect(row["longUrl"], max_age=_cache_seconds(row))


def _cache_seconds(row):
    """How long a client may cache this redirect.

    Capped at the link's remaining lifetime, so the cache entry never outlives
    the link it points at. A link with twenty seconds left is cached for twenty
    seconds, not sixty.
    """
    expires_at = row.get("expiresAt")
    if expires_at is None:
        return _MAX_CACHE_SECONDS
    remaining = int(expires_at) - int(time.time())
    return max(0, min(_MAX_CACHE_SECONDS, remaining))


def _record_analytics(event, short_code):
    """Persist a click event and emit faceted metrics. Never raises."""
    try:
        country = geo.get_country(event)
        referrer = geo.get_referrer(event)
        user_agent = geo.get_user_agent(event)

        dynamo.record_click(
            short_code,
            country=country,
            referrer=referrer,
            user_agent=user_agent,
        )
        dynamo.increment_click_count(short_code)

        metrics.count("ClicksByCountry", dimensions={"Country": country})
        metrics.count("ClicksByReferrer", dimensions={"Referrer": referrer})
    except Exception as exc:  # noqa: BLE001
        # Deliberate blanket catch. Analytics is a side effect of the user's
        # request, not the request itself, so it must never break a redirect.
        # Do not narrow this without reading the note in the module docstring.
        print(f"[warn] failed to record analytics for {short_code}: {exc}")
