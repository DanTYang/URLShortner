"""GET /urls/{shortCode}/analytics — aggregated stats for one short link.

Returns the cached total click count plus breakdowns computed on the fly from
the raw click events: clicks by country, by referrer, and a daily time series,
along with the most recent individual clicks.

Response (JSON)::

    {
      "shortCode": "abc1234",
      "longUrl": "https://example.com/...",
      "totalClicks": 42,
      "createdAt": "2026-07-01T12:00:00+00:00",
      "expiresAt": 1793664000,
      "clicksByCountry": {"US": 30, "GB": 8, "UNKNOWN": 4},
      "clicksByReferrer": {"t.co": 20, "direct": 15, "news.ycombinator.com": 7},
      "clicksByDay": {"2026-07-18": 10, "2026-07-19": 32},
      "recentClicks": [ {...}, ... ]
    }
"""

from collections import Counter

from urlshortener_common import dynamo, responses

# How many click rows to aggregate over. Bounds work per request; the cached
# ``clickCount`` on the URL row is always the authoritative lifetime total.
_MAX_EVENTS = 5000
_RECENT_LIMIT = 25


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    short_code = path_params.get("shortCode", "")
    if not short_code:
        return responses.error(400, "Missing short code.")

    row = dynamo.get_url(short_code)
    if row is None:
        return responses.error(404, "Short link not found.")

    by_country: Counter = Counter()
    by_referrer: Counter = Counter()
    by_day: Counter = Counter()
    recent: list[dict] = []

    for i, click in enumerate(dynamo.query_clicks(short_code, limit=_MAX_EVENTS)):
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
            "expiresAt": row.get("expiresAt"),
            # Authoritative lifetime total from the atomic counter.
            "totalClicks": int(row.get("clickCount", 0)),
            # Breakdowns, sorted most-frequent first for easy display.
            "clicksByCountry": dict(by_country.most_common()),
            "clicksByReferrer": dict(by_referrer.most_common()),
            "clicksByDay": dict(sorted(by_day.items())),
            "recentClicks": recent,
        },
    )
