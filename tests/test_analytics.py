"""Tests for the analytics and list endpoints."""

import json

from conftest import api_event, load_handler


def _create(body):
    return json.loads(
        load_handler("create_url").handler(api_event(body=json.dumps(body)), None)["body"]
    )


def _redirect(code, **headers):
    load_handler("redirect").handler(
        api_event(pathParameters={"shortCode": code}, headers={"Host": "x", **headers}),
        None,
    )


def _analytics(code):
    app = load_handler("get_analytics")
    return app.handler(api_event(pathParameters={"shortCode": code}), None)


def test_analytics_aggregates_by_facet(aws):
    created = _create({"url": "https://example.com"})
    code = created["shortCode"]

    _redirect(code, **{"CloudFront-Viewer-Country": "US", "Referer": "https://t.co/a"})
    _redirect(code, **{"CloudFront-Viewer-Country": "US"})
    _redirect(code, **{"CloudFront-Viewer-Country": "GB", "Referer": "https://t.co/b"})

    resp = _analytics(code)
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])

    assert data["totalClicks"] == 3
    assert data["clicksByCountry"] == {"US": 2, "GB": 1}
    assert data["clicksByReferrer"]["t.co"] == 2
    assert data["clicksByReferrer"]["direct"] == 1
    # Daily series present and totals to the number of clicks.
    assert sum(data["clicksByDay"].values()) == 3
    assert len(data["recentClicks"]) == 3


def test_analytics_unknown_code_404(aws):
    assert _analytics("missing")["statusCode"] == 404


def test_list_urls_returns_created_links(aws):
    _create({"url": "https://a.com", "owner": "u1"})
    _create({"url": "https://b.com", "owner": "u1"})
    _create({"url": "https://c.com", "owner": "u2"})

    app = load_handler("list_urls")

    # All links (scan path).
    resp = app.handler(api_event(queryStringParameters={}), None)
    assert json.loads(resp["body"])["count"] == 3

    # Filtered by owner (GSI path).
    resp = app.handler(api_event(queryStringParameters={"owner": "u1"}), None)
    body = json.loads(resp["body"])
    assert body["count"] == 2
    assert {i["longUrl"] for i in body["items"]} == {"https://a.com", "https://b.com"}
