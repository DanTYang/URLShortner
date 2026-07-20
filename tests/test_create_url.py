"""Integration-style tests for the create-URL handler (mocked DynamoDB)."""

import json

from conftest import api_event, load_handler


def _invoke(body):
    app = load_handler("create_url")
    return app.handler(api_event(body=json.dumps(body)), None)


def test_create_auto_code(aws):
    resp = _invoke({"url": "https://example.com/a/b/c"})
    assert resp["statusCode"] == 201
    payload = json.loads(resp["body"])
    assert payload["longUrl"] == "https://example.com/a/b/c"
    assert len(payload["shortCode"]) >= 7
    assert payload["shortUrl"].endswith(payload["shortCode"])
    # Row was actually persisted.
    item = aws["urls"].get_item(Key={"shortCode": payload["shortCode"]})["Item"]
    assert item["longUrl"] == "https://example.com/a/b/c"
    assert item["clickCount"] == 0


def test_create_custom_code(aws):
    resp = _invoke({"url": "https://example.com", "customCode": "launch"})
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["shortCode"] == "launch"


def test_custom_code_collision_returns_409(aws):
    _invoke({"url": "https://example.com", "customCode": "dupe"})
    resp = _invoke({"url": "https://other.com", "customCode": "dupe"})
    assert resp["statusCode"] == 409


def test_missing_url_returns_400(aws):
    assert _invoke({})["statusCode"] == 400


def test_invalid_url_scheme_returns_400(aws):
    assert _invoke({"url": "ftp://example.com"})["statusCode"] == 400
    assert _invoke({"url": "not-a-url"})["statusCode"] == 400


def test_invalid_custom_code_returns_400(aws):
    resp = _invoke({"url": "https://example.com", "customCode": "no spaces"})
    assert resp["statusCode"] == 400


def test_expiry_is_persisted(aws):
    resp = _invoke({"url": "https://example.com", "expiresInDays": 30})
    payload = json.loads(resp["body"])
    assert payload["expiresAt"] is not None
    item = aws["urls"].get_item(Key={"shortCode": payload["shortCode"]})["Item"]
    # Both the exact-check attribute and the DynamoDB TTL attribute are set.
    assert int(item["expiresAt"]) == payload["expiresAt"]
    assert int(item["ttl"]) == payload["expiresAt"]


def test_bad_expiry_returns_400(aws):
    assert _invoke({"url": "https://example.com", "expiresInDays": -1})["statusCode"] == 400
    assert _invoke({"url": "https://example.com", "expiresInDays": 99999})["statusCode"] == 400
