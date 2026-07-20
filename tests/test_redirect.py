"""Tests for the redirect handler and its analytics side effects."""

import json
import time

from conftest import api_event, load_handler


def _create(body):
    return json.loads(
        load_handler("create_url").handler(api_event(body=json.dumps(body)), None)["body"]
    )


def _redirect(short_code, **headers):
    app = load_handler("redirect")
    event = api_event(
        pathParameters={"shortCode": short_code},
        headers={"Host": "x", **headers},
    )
    return app.handler(event, None)


def test_redirect_hits_target(aws):
    created = _create({"url": "https://example.com/target"})
    resp = _redirect(created["shortCode"])
    assert resp["statusCode"] == 301
    assert resp["headers"]["Location"] == "https://example.com/target"


def test_redirect_unknown_code_404(aws):
    assert _redirect("nope123")["statusCode"] == 404


def test_redirect_expired_link_410(aws):
    created = _create({"url": "https://example.com"})
    # Force the stored link to have expired in the past.
    aws["urls"].update_item(
        Key={"shortCode": created["shortCode"]},
        UpdateExpression="SET expiresAt = :e",
        ExpressionAttributeValues={":e": int(time.time()) - 10},
    )
    assert _redirect(created["shortCode"])["statusCode"] == 410


def test_redirect_records_click_and_increments_counter(aws):
    created = _create({"url": "https://example.com"})
    code = created["shortCode"]

    _redirect(code, **{"CloudFront-Viewer-Country": "GB", "Referer": "https://t.co/x"})
    _redirect(code, **{"CloudFront-Viewer-Country": "US"})

    # Counter on the URL row.
    row = aws["urls"].get_item(Key={"shortCode": code})["Item"]
    assert int(row["clickCount"]) == 2

    # Two click rows persisted, with the facets we sent.
    clicks = aws["clicks"].query(
        KeyConditionExpression="shortCode = :c",
        ExpressionAttributeValues={":c": code},
    )["Items"]
    assert len(clicks) == 2
    countries = {c["country"] for c in clicks}
    assert countries == {"GB", "US"}
    referrers = {c["referrer"] for c in clicks}
    assert "t.co" in referrers
    assert "direct" in referrers


def test_analytics_failure_does_not_break_redirect(aws, monkeypatch):
    """Even if recording analytics throws, the redirect still succeeds."""
    created = _create({"url": "https://example.com"})
    from urlshortener_common import dynamo

    def boom(*a, **k):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(dynamo, "record_click", boom)
    resp = _redirect(created["shortCode"])
    assert resp["statusCode"] == 301
