"""Helpers for building API Gateway (REST, proxy integration) responses.

Every Lambda behind API Gateway proxy integration must return a dict with
``statusCode``, ``headers`` and a (string) ``body``. Centralising that shape
here keeps the handlers terse and the JSON/CORS handling consistent.
"""

import json

_JSON_HEADERS = {
    "Content-Type": "application/json",
    # CORS: allow browser dashboards on any origin to call the API.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}


def to_int(value):
    """Coerce a DynamoDB number to a plain ``int``, passing ``None`` through.

    DynamoDB returns every number as ``decimal.Decimal``, which ``json.dumps``
    refuses to serialise. :func:`json_response` passes ``default=str`` so that
    never raises -- but the cost is that an unconverted number is published as
    a JSON *string* instead of a number.

    That is how the same field ends up as ``1788725445`` from one endpoint and
    ``"1788725445"`` from another: whichever handler happened to hold a real
    Python int got a number, and the ones reading straight from DynamoDB got
    text. Any client doing arithmetic on it breaks on half your API.

    Use this on every numeric value that comes out of a table row.
    """
    return None if value is None else int(value)


def json_response(status_code, body):
    """Build a JSON API response."""
    return {
        "statusCode": status_code,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(body, default=str),
    }


def error(status_code, message, **extra):
    """Build a JSON error response of the form ``{"error": "..."}``."""
    payload = {"error": message}
    payload.update(extra)
    return json_response(status_code, payload)


def redirect(location, *, status_code = 302, max_age = 60):
    """Build an HTTP redirect response.

    Defaults to 302 rather than 301. Every link in this service can be deleted
    or can expire, so no redirect is genuinely permanent, and a 301 asserts
    otherwise. Some intermediaries cache a 301 indefinitely regardless of
    Cache-Control, which cannot be risked once links are revocable.

    ``max_age`` is the caller's staleness budget, not a performance knob. Once
    a response is cached there is no mechanism to revoke it, so this value is
    the worst case for how long a deleted or expired link keeps resolving.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Location": location,
            "Cache-Control": f"public, max-age={max_age}",
        },
        "body": "",
    }


def parse_json_body(event):
    """Parse a JSON request body, returning ``{}`` when absent/blank.

    Raises
    ------
    ValueError
        If the body is present but not valid JSON.
    """
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object.")
    return parsed
