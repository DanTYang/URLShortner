"""Helpers for building API Gateway (REST, proxy integration) responses.

Every Lambda behind API Gateway proxy integration must return a dict with
``statusCode``, ``headers`` and a (string) ``body``. Centralising that shape
here keeps the handlers terse and the JSON/CORS handling consistent.
"""

import json
from typing import Any

_JSON_HEADERS = {
    "Content-Type": "application/json",
    # CORS: allow browser dashboards on any origin to call the API.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}


def json_response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON API response."""
    return {
        "statusCode": status_code,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(body, default=str),
    }


def error(status_code: int, message: str, **extra: Any) -> dict[str, Any]:
    """Build a JSON error response of the form ``{"error": "..."}``."""
    payload = {"error": message}
    payload.update(extra)
    return json_response(status_code, payload)


def redirect(location: str, *, status_code: int = 301) -> dict[str, Any]:
    """Build an HTTP redirect response.

    Defaults to 301 (permanent) so browsers and crawlers cache the mapping,
    which offloads repeat traffic from Lambda entirely.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Location": location,
            "Cache-Control": "public, max-age=86400",
        },
        "body": "",
    }


def parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
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
