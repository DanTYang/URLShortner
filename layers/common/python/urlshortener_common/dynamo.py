"""DynamoDB access layer.

All reads and writes to the two tables funnel through this module so that the
handlers stay focused on HTTP concerns and the storage details (key shapes,
conditional writes, TTL maths) live in one place.

Table shapes
------------
UrlsTable   PK ``shortCode``            — one row per short link.
            GSI ``owner-createdAt-index`` for "list my links, newest first".
ClicksTable PK ``shortCode`` / SK ``clickId`` — one row per click event.
"""

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from . import config

# Reuse a single resource across warm invocations. Creating the client is the
# expensive part, so hoisting it out of the handler cuts cold-start-adjacent
# latency on every subsequent call.
_dynamodb = boto3.resource("dynamodb")
_urls_table = _dynamodb.Table(config.URLS_TABLE)
_clicks_table = _dynamodb.Table(config.CLICKS_TABLE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """Current time as an ISO-8601 UTC string (sortable, human readable)."""
    return _now().isoformat()


# ---------------------------------------------------------------------------
# URL rows
# ---------------------------------------------------------------------------
def put_url(
    short_code: str,
    long_url: str,
    *,
    owner: str = "anonymous",
    expires_at: Optional[int] = None,
    custom: bool = False,
) -> bool:
    """Atomically create a URL row, failing if the code is already taken.

    The ``ConditionExpression`` is the entire collision-detection mechanism:
    DynamoDB guarantees the write is rejected if a row with the same
    ``shortCode`` already exists, even under concurrent writers. That makes
    short-code uniqueness a property of the database, not a check-then-write
    race in application code.

    Returns
    -------
    ``True``  if the row was created.
    ``False`` if the code already existed (collision) — the caller retries.
    """
    item: dict[str, Any] = {
        "shortCode": short_code,
        "longUrl": long_url,
        "owner": owner,
        "createdAt": iso_now(),
        "clickCount": 0,
        "custom": custom,
    }
    if expires_at is not None:
        # Stored twice: ``expiresAt`` for our own exact expiry check on read,
        # and ``ttl`` for DynamoDB's automatic (eventual) background deletion.
        item["expiresAt"] = expires_at
        item["ttl"] = expires_at

    try:
        _urls_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(shortCode)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False  # collision — caller should retry with a new code
        raise


def get_url(short_code: str) -> Optional[dict[str, Any]]:
    """Fetch a URL row by code, or ``None`` if it does not exist."""
    resp = _urls_table.get_item(Key={"shortCode": short_code})
    return resp.get("Item")


def increment_click_count(short_code: str) -> int:
    """Atomically bump the cached click counter and return the new value."""
    resp = _urls_table.update_item(
        Key={"shortCode": short_code},
        UpdateExpression="SET clickCount = if_not_exists(clickCount, :zero) + :one",
        ExpressionAttributeValues={":one": 1, ":zero": 0},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["clickCount"])


def list_urls(
    owner: Optional[str] = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List URL rows.

    With an ``owner`` this queries the GSI (efficient, sorted newest-first).
    Without one it falls back to a bounded scan — fine for a demo/admin view
    but explicitly capped so it can never run away on a large table.
    """
    if owner:
        resp = _urls_table.query(
            IndexName="owner-createdAt-index",
            KeyConditionExpression=Key("owner").eq(owner),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        return resp.get("Items", [])

    resp = _urls_table.scan(Limit=limit)
    return resp.get("Items", [])


def is_expired(url_row: dict[str, Any], *, now: Optional[int] = None) -> bool:
    """True if the link has an ``expiresAt`` in the past.

    DynamoDB TTL deletion is only eventual (it can lag by up to ~48h), so we
    also enforce expiry here on read to guarantee an expired link stops
    resolving immediately.
    """
    expires_at = url_row.get("expiresAt")
    if expires_at is None:
        return False
    current = now if now is not None else int(time.time())
    return int(expires_at) <= current


# ---------------------------------------------------------------------------
# Click events
# ---------------------------------------------------------------------------
def record_click(
    short_code: str,
    *,
    country: str = "UNKNOWN",
    referrer: str = "direct",
    user_agent: str = "",
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """Write one click event to the analytics table.

    The sort key ``clickId`` is ``<iso-timestamp>#<uuid>`` so events sort
    chronologically and never collide even within the same millisecond. Each
    row carries its own ``ttl`` so old raw events are purged automatically
    after ``CLICK_RETENTION_DAYS``.
    """
    ts = timestamp or _now()
    click_id = f"{ts.isoformat()}#{uuid.uuid4().hex[:8]}"
    ttl = int(ts.timestamp()) + config.CLICK_RETENTION_DAYS * 86400

    item = {
        "shortCode": short_code,
        "clickId": click_id,
        "timestamp": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
        "country": country,
        "referrer": referrer,
        "userAgent": user_agent[:512],  # cap to avoid unbounded rows
        "ttl": ttl,
    }
    _clicks_table.put_item(Item=item)
    return item


def query_clicks(
    short_code: str,
    *,
    limit: int = 1000,
) -> Iterable[dict[str, Any]]:
    """Yield click events for a link, paginating transparently."""
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("shortCode").eq(short_code),
        "ScanIndexForward": False,  # newest first
    }
    remaining = limit
    while remaining > 0:
        kwargs["Limit"] = min(remaining, 1000)
        resp = _clicks_table.query(**kwargs)
        items = resp.get("Items", [])
        for item in items:
            yield item
        remaining -= len(items)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
