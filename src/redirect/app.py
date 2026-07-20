"""GET /{shortCode} — resolve a short code and redirect to the target URL.

This is the hot path — the endpoint that receives essentially all production
traffic — so it does the minimum necessary work:

1. One DynamoDB ``GetItem`` to resolve the code.
2. Enforce expiry (return 410 if the link has lapsed).
3. Record analytics (a click row + an atomic counter increment) and emit
   CloudWatch metrics sliced by country and referrer.
4. Return a cacheable 301 redirect.

Analytics is intentionally recorded inline and best-effort: if the click write
fails we still serve the redirect, because resolving the link is the user's
goal and losing one analytics event is preferable to a failed redirect.
"""

from urlshortener_common import dynamo, geo, metrics, responses


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    short_code = path_params.get("shortCode", "")

    if not short_code:
        return responses.error(400, "Missing short code.")

    row = dynamo.get_url(short_code)

    # ---- Not found --------------------------------------------------------
    if row is None:
        metrics.count("RedirectNotFound")
        return responses.error(404, "Short link not found.")

    # ---- Expired ----------------------------------------------------------
    if dynamo.is_expired(row):
        metrics.count("RedirectExpired")
        return responses.error(410, "This short link has expired.")

    long_url = row["longUrl"]

    # ---- Record analytics (best effort) -----------------------------------
    _record_analytics(event, short_code)

    # ---- Redirect ---------------------------------------------------------
    metrics.count("Redirects")
    metrics.count("RedirectHit")
    return responses.redirect(long_url, status_code=301)


def _record_analytics(event, short_code: str) -> None:
    """Persist a click event and emit faceted CloudWatch metrics.

    Failures here are swallowed: analytics must never break a redirect.
    """
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

        # Faceted metrics let the CloudWatch dashboard break clicks down by
        # geography and traffic source without querying the table.
        metrics.count("ClicksByCountry", dimensions={"Country": country})
        metrics.count("ClicksByReferrer", dimensions={"Referrer": referrer})
    except Exception as exc:  # noqa: BLE001 — analytics is best-effort
        # Log for debugging but never fail the redirect.
        print(f"[warn] failed to record analytics for {short_code}: {exc}")
