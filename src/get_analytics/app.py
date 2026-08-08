"""GET /urls/{shortCode}/analytics — aggregated statistics for one link.

Two-tier accounting
-------------------
``totalClicks`` is read from an atomic counter on the URL row. It is
authoritative, costs nothing extra to read, and survives forever.

The breakdowns are computed on the fly from raw click events, which expire
after ``CLICK_RETENTION_DAYS``. So lifetime totals are kept permanently without
paying to store every individual event permanently — recent data is detailed,
old data is summarised.

Known limitation: aggregation is O(clicks) per request, bounded at
``_MAX_EVENTS``. The response reports ``truncated`` when that bound is hit. At
scale the right design is to roll clicks into daily counters as they arrive
(via DynamoDB Streams) and have this endpoint read the rollups.
"""

from collections import Counter

from urlshortener_common import dynamo, responses

# Bounds the work per request so one viral link cannot exhaust the function's
# memory or time budget. The cached counter on the URL row remains the
# authoritative lifetime total regardless.
_MAX_EVENTS = 5000
_RECENT_LIMIT = 25


def handler(event, context):
    """Return click totals and breakdowns for one short link.

    Returns 400 if the short code is missing and 404 if it is unknown.
    """
    short_code = (event.get("pathParameters") or {}).get("shortCode", "")
    if not short_code:
        return responses.error(400, "Missing short code.")

    row = dynamo.get_url(short_code)
    if row is None:
        return responses.error(404, "Short link not found.")

    by_country = Counter()
    by_referrer = Counter()
    by_day = Counter()
    recent = []
    events_read = 0

    for i, click in enumerate(dynamo.query_clicks(short_code, limit=_MAX_EVENTS)):
        events_read = i + 1
        # .get with defaults, not indexing: rows written before a field existed
        # are normal in a schemaless store and must not break the reader.
        by_country[click.get("country", "UNKNOWN")] += 1
        by_referrer[click.get("referrer", "direct")] += 1
        by_day[click.get("date", "unknown")] += 1
        if i < _RECENT_LIMIT:
            recent.append(
                {
                    "timestamp": click.get("timestamp"),
                    "country": click.get("country"),
                    "referrer": click.get("referrer"),
                }
            )

    return responses.json_response(
        200,
        {
            "shortCode": short_code,
            "longUrl": row.get("longUrl"),
            "owner": row.get("owner"),
            "createdAt": row.get("createdAt"),
            "expiresAt": responses.to_int(row.get("expiresAt")),
            "totalClicks": int(row.get("clickCount", 0)),
            # Facets sorted most-frequent-first so a client can render in order;
            # the daily series sorted chronologically, because a time series
            # ordered by magnitude is meaningless.
            "clicksByCountry": dict(by_country.most_common()),
            "clicksByReferrer": dict(by_referrer.most_common()),
            "clicksByDay": dict(sorted(by_day.items())),
            "recentClicks": recent,
            # Declare the bound. Without this a caller sees totalClicks=10000
            # beside a daily series summing to 5000 and cannot tell whether the
            # gap is this cap, the retention window, or a bug.
            "eventsAnalysed": events_read,
            "truncated": events_read >= _MAX_EVENTS,
        },
    )
