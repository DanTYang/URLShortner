"""GET /urls — list short links.

Query parameters
----------------
owner   Restrict to one owner. Uses the ``owner-createdAt-index`` GSI.
limit   Maximum rows to return (1-100, default 50).

Without an ``owner`` this performs a bounded table scan. That is acceptable for
an admin or demo view because the cap is hard, but a scan's cost grows with the
size of the whole table rather than with the size of the result — so per-user
listings should always pass ``owner`` and take the index path.

Known limitation: ``owner`` is supplied by the caller and there is no
authentication in front of this endpoint, so it groups links rather than
protecting them. See docs/ARCHITECTURE.md#security-posture.
"""

from urlshortener_common import dynamo, responses

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


def handler(event, context):
    """List links, optionally filtered to a single owner.

    Returns 400 if ``limit`` is not an integer. Out-of-range limits are clamped
    rather than rejected: a non-numeric limit has no sensible interpretation,
    whereas ``limit=99999`` clearly means "as many as you'll give me".

    Rows are projected field by field rather than returned verbatim. The stored
    schema and the public response are separate contracts, and an explicit
    projection is what stops an internal attribute from being published by
    accident.
    """
    params = event.get("queryStringParameters") or {}

    # `or None` matters: an empty string would reach the GSI query and DynamoDB
    # rejects an empty key value outright.
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
            # to_int, not the raw value: DynamoDB returns numbers as Decimal and
            # json_response falls back to str(), which would publish this field
            # as a JSON string here and a number from POST /urls.
            "expiresAt": responses.to_int(r.get("expiresAt")),
            "clickCount": int(r.get("clickCount", 0)),
            "custom": bool(r.get("custom", False)),
        }
        for r in rows
    ]

    return responses.json_response(
        200,
        {
            "count": len(items),
            "owner": owner,
            "items": items,
        },
    )
