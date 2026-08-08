"""DynamoDB access layer.

All reads and writes funnel through this module, so handlers stay focused on
HTTP and the storage details — key shapes, conditional writes, TTL maths — live
in one place.

Table shapes
------------
UrlsTable   PK ``shortCode``. One row per short link.
            GSI ``owner-createdAt-index`` for "list my links, newest first".
ClicksTable PK ``shortCode`` / SK ``clickId``. One row per click event.

The table handles below are created at module scope on purpose. Lambda reuses a
warm container across invocations, so module-level work runs once and is then
free for every subsequent request; building the client inside the handler would
pay that cost on every redirect. (The trade-off is that anything wanting to
substitute these handles has to rebind the module globals.)
"""

import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from . import config

_dynamodb = boto3.resource("dynamodb")
_urls_table = _dynamodb.Table(config.URLS_TABLE)
_clicks_table = _dynamodb.Table(config.CLICKS_TABLE)


def _now():
    """Timezone-aware current UTC time. Always store UTC; never ``utcnow()``."""
    return datetime.now(timezone.utc)


def iso_now():
    """Current time as an ISO-8601 UTC string — sortable and human-readable."""
    return _now().isoformat()


# ---------------------------------------------------------------------------
# URL rows
# ---------------------------------------------------------------------------
def put_url(short_code, long_url, *, owner="anonymous", expires_at=None, custom=False):
    """Atomically create a URL row, failing if the code is already taken.

    The ``ConditionExpression`` is the entire collision-detection mechanism.
    DynamoDB evaluates it atomically at the storage node, so exactly one writer
    wins under any amount of concurrency. Uniqueness becomes a property of the
    database rather than a check-then-write race in application code.

    Returns
    -------
    True if the row was created, False if the code already existed — in which
    case the caller retries with a fresh candidate.
    """
    url_row = {
        "shortCode": short_code,
        "longUrl": long_url,
        "owner": owner,
        "createdAt": iso_now(),
        "clickCount": 0,
        "custom": custom,
    }
    if expires_at is not None:
        # Stored twice, two jobs: `expiresAt` drives the exact check on read,
        # `ttl` drives DynamoDB's background deletion. TTL alone is eventual
        # (AWS documents up to 48 hours), so a lapsed link would keep resolving.
        url_row["expiresAt"] = expires_at
        url_row["ttl"] = expires_at

    try:
        _urls_table.put_item(
            Item=url_row,
            ConditionExpression="attribute_not_exists(shortCode)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        # Any other error is a real failure. Returning False here would send
        # the caller into a pointless retry loop against a broken database.
        raise


def get_url(short_code):
    """Fetch a URL row by code, or None if it does not exist.

    A miss is an absent ``Item`` key, not an exception.
    """
    return _urls_table.get_item(Key={"shortCode": short_code}).get("Item")


def increment_click_count(short_code):
    """Atomically bump the cached click counter and return the new value.

    Read-modify-write in Python would lose clicks: two concurrent requests both
    read 5 and both write 6. An update expression is evaluated at the storage
    node under a per-item lock, and ``ReturnValues`` hands back *this* caller's
    result rather than whatever the counter reads a moment later.
    """
    resp = _urls_table.update_item(
        Key={"shortCode": short_code},
        UpdateExpression="SET clickCount = if_not_exists(clickCount, :zero) + :one",
        ExpressionAttributeValues={":one": 1, ":zero": 0},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["clickCount"])


def list_urls(owner=None, *, limit=50):
    """List URL rows, optionally filtered to one owner.

    With an owner this queries the GSI and reads only that owner's partition,
    so cost scales with their link count. Without one it falls back to a
    bounded scan, whose cost scales with the size of the entire table — fine
    for an admin view behind a hard cap, wrong for a hot path.
    """
    if owner:
        resp = _urls_table.query(
            IndexName="owner-createdAt-index",
            KeyConditionExpression=Key("owner").eq(owner),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
    else:
        resp = _urls_table.scan(Limit=limit)
    return resp.get("Items", [])


def is_expired(url_row, *, now=None):
    """True if the link carries an ``expiresAt`` in the past.

    ``now`` is injectable so tests can simulate the future without sleeping.
    Compared with ``is None`` rather than truthiness, because epoch 0 is a
    legitimate instant.
    """
    expires_at = url_row.get("expiresAt")
    if expires_at is None:
        return False
    now = int(time.time()) if now is None else now
    return expires_at <= now


# ---------------------------------------------------------------------------
# Click events
# ---------------------------------------------------------------------------
def record_click(
    short_code, *, country="UNKNOWN", referrer="direct", user_agent="", timestamp=None
):
    """Write one click event to the analytics table.

    Three deliberate choices:

    * ``clickId`` is ``<iso-timestamp>#<uuid8>``. The timestamp makes the sort
      key chronological, so "most recent clicks" is a range read rather than a
      sort; the random suffix stops two clicks in the same microsecond from
      sharing a key and overwriting each other.
    * ``date`` is denormalised from ``timestamp`` so the daily series is a
      group-by rather than a parse of every row.
    * ``userAgent`` is truncated. Never store an unbounded attacker-supplied
      string in an item capped at 400 KB and billed by the byte.
    """
    ts = timestamp or _now()
    click = {
        "shortCode": short_code,
        "clickId": f"{ts.isoformat()}#{uuid.uuid4().hex[:8]}",
        "timestamp": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
        "country": country,
        "referrer": referrer,
        "userAgent": user_agent[:512],
        "ttl": int(ts.timestamp()) + config.CLICK_RETENTION_DAYS * 86400,
    }
    _clicks_table.put_item(Item=click)
    return click


def query_clicks(short_code, *, limit=1000):
    """Yield click events for a link, newest first, paginating transparently.

    DynamoDB caps a single Query at 1 MB and returns a ``LastEvaluatedKey``
    bookmark when there is more. Callers should not have to know that, so it is
    hidden here.

    This is a generator: a caller that stops early never triggers the next
    page, and the full result set never has to fit in memory.
    """
    kwargs = {
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
