"""GET /urls — list short links.

Query string parameters:
  * ``owner``  — if given, list only that owner's links (via the GSI).
  * ``limit``  — max rows to return (1-100, default 50).

Without an ``owner`` this performs a bounded scan, which is fine for an admin
or demo view but is intentionally capped so it can't run away on a big table.
For per-user listings always pass ``owner`` so the efficient index is used.
"""

from urlshortener_common import dynamo, responses

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    owner = (params.get("owner") or "").strip() or None

    try:
        limit = int(params.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return responses.error(400, "limit must be an integer.")
    limit = max(1, min(limit, _MAX_LIMIT))

    rows = dynamo.list_urls(owner=owner, limit=limit)

    items = [
        {
            "shortCode": r.get("shortCode"),
            "longUrl": r.get("longUrl"),
            "owner": r.get("owner"),
            "createdAt": r.get("createdAt"),
            "expiresAt": r.get("expiresAt"),
            "clickCount": int(r.get("clickCount", 0)),
            "custom": bool(r.get("custom", False)),
        }
        for r in rows
    ]

    return responses.json_response(
        200, {"count": len(items), "owner": owner, "items": items}
    )
