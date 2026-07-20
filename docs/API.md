# API Reference

Base URL: the `ApiBaseUrl` printed in the SAM deploy outputs, e.g.
`https://abc123.execute-api.us-east-1.amazonaws.com/dev`. All request/response
bodies are JSON. CORS is open (`*`) so browser dashboards can call the API.

| Method & path | Purpose |
|---|---|
| [`POST /urls`](#post-urls) | Create a short link |
| [`GET /{shortCode}`](#get-shortcode) | Resolve a code and redirect |
| [`GET /urls/{shortCode}/analytics`](#get-urlsshortcodeanalytics) | Per-link analytics |
| [`GET /urls`](#get-urls) | List links |

---

## `POST /urls`

Create a short link.

### Request body
| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | ✅ | Absolute `http(s)` URL to shorten. |
| `customCode` | string | — | Desired code: 3–64 chars of `A–Z a–z 0–9 _ -`, not a reserved word. |
| `expiresInDays` | number | — | Positive; link expires this many days from now (max 3650). |
| `owner` | string | — | Arbitrary owner id for grouping/listing (default `anonymous`). |

### Responses
| Status | When |
|---|---|
| `201 Created` | Link created. |
| `400 Bad Request` | Missing/invalid `url`, bad `customCode`, or bad `expiresInDays`. |
| `409 Conflict` | The requested `customCode` is already taken. |
| `503 Service Unavailable` | Could not allocate a unique code after retries (extremely rare). |

### Example
```bash
curl -s -X POST "$API/urls" \
  -H 'Content-Type: application/json' \
  -d '{
        "url": "https://aws.amazon.com/lambda/",
        "customCode": "lambda",
        "expiresInDays": 30,
        "owner": "user-123"
      }'
```
```json
{
  "shortCode": "lambda",
  "shortUrl": "https://abc123.execute-api.us-east-1.amazonaws.com/dev/lambda",
  "longUrl": "https://aws.amazon.com/lambda/",
  "owner": "user-123",
  "custom": true,
  "expiresAt": 1793664000,
  "createdAt": "2026-07-20T12:00:00+00:00"
}
```
`expiresAt` is epoch seconds, or `null` when the link never expires.

---

## `GET /{shortCode}`

Resolve a short code and redirect to its target. This is the endpoint a short
link points at.

### Responses
| Status | Meaning | Headers |
|---|---|---|
| `301 Moved Permanently` | Found; redirecting. | `Location: <longUrl>`, `Cache-Control: public, max-age=86400` |
| `404 Not Found` | No such code. | — |
| `410 Gone` | The link existed but has expired. | — |

Side effect: a successful redirect records a click event (country, referrer,
timestamp), atomically increments the link's `clickCount`, and emits
CloudWatch metrics. Analytics is best-effort and never blocks the redirect.

### Example
```bash
# -L follows the redirect to the destination
curl -sL "$API/lambda"

# See just the redirect itself
curl -si "$API/lambda" | head -n 3
#   HTTP/2 301
#   location: https://aws.amazon.com/lambda/
#   cache-control: public, max-age=86400
```

> **Geography:** country is read from the `CloudFront-Viewer-Country` header,
> which AWS injects automatically when the API is fronted by CloudFront. Behind
> a bare API Gateway URL there is no such header, so country records as
> `UNKNOWN` (you can inject `X-Country` for testing). See
> [`ANALYTICS.md`](ANALYTICS.md#geography).

---

## `GET /urls/{shortCode}/analytics`

Aggregated analytics for one link.

### Responses
| Status | When |
|---|---|
| `200 OK` | Analytics returned. |
| `404 Not Found` | No such code. |

### Example
```bash
curl -s "$API/urls/lambda/analytics" | jq
```
```json
{
  "shortCode": "lambda",
  "longUrl": "https://aws.amazon.com/lambda/",
  "owner": "user-123",
  "createdAt": "2026-07-20T12:00:00+00:00",
  "expiresAt": 1793664000,
  "totalClicks": 42,
  "clicksByCountry": { "US": 30, "GB": 8, "UNKNOWN": 4 },
  "clicksByReferrer": { "t.co": 20, "direct": 15, "news.ycombinator.com": 7 },
  "clicksByDay": { "2026-07-18": 10, "2026-07-19": 32 },
  "recentClicks": [
    { "timestamp": "2026-07-19T22:14:03+00:00", "country": "US", "referrer": "t.co" }
  ]
}
```
Notes:
- `totalClicks` is the authoritative lifetime counter (survives click-event TTL).
- The breakdowns are computed from raw click events within the retention window
  (`ClickRetentionDays`, default 90), over at most the most recent 5,000 events.

---

## `GET /urls`

List links.

### Query parameters
| Param | Type | Default | Description |
|---|---|---|---|
| `owner` | string | — | If given, list only this owner's links (uses the GSI, newest first). |
| `limit` | number | 50 | Max rows (1–100). |

Without `owner`, the endpoint performs a **bounded scan** (capped by `limit`) —
convenient for an admin/demo view but not intended for large-scale pagination.
For per-user listings always pass `owner`.

### Responses
| Status | When |
|---|---|
| `200 OK` | List returned. |
| `400 Bad Request` | `limit` is not an integer. |

### Example
```bash
curl -s "$API/urls?owner=user-123&limit=25" | jq
```
```json
{
  "count": 2,
  "owner": "user-123",
  "items": [
    {
      "shortCode": "lambda",
      "longUrl": "https://aws.amazon.com/lambda/",
      "owner": "user-123",
      "createdAt": "2026-07-20T12:00:00+00:00",
      "expiresAt": 1793664000,
      "clickCount": 42,
      "custom": true
    }
  ]
}
```

---

## Error format

All error responses share one shape:
```json
{ "error": "Human-readable explanation." }
```
`409` conflicts and `400` validation errors include enough detail to correct
the request.
