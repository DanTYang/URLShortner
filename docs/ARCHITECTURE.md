# Architecture

This document explains **how the service is built and why**, so you can reason
about it (and re-explain it) without re-reading every source file. Read it
top-to-bottom the first time; after that use it as a reference.

- [The big picture](#the-big-picture)
- [Request flows](#request-flows)
- [Data model](#data-model)
- [Key design decisions](#key-design-decisions)
- [Cost & scaling](#cost--scaling)
- [Security posture](#security-posture)
- [Failure modes & how they're handled](#failure-modes--how-theyre-handled)

---

## The big picture

The whole system is **serverless** — there are no servers, containers, or
load balancers to manage. Three AWS services do all the work:

| Layer | Service | Responsibility |
|---|---|---|
| **Edge / routing** | **API Gateway** (REST) | Terminates HTTPS, routes each path+method to a Lambda, throttles abuse. |
| **Compute** | **AWS Lambda** (Python 3.12, arm64) | Four small single-purpose functions holding all business logic. |
| **Storage** | **DynamoDB** (two tables) | Durable, horizontally-scalable key-value storage for links and clicks. |
| **Observability** | **CloudWatch** | Custom metrics (emitted as EMF logs), a dashboard, and an alarm. |

Everything is described in one `template.yaml` (AWS SAM), so the entire stack
is version-controlled and reproducible.

Why serverless for this problem? A URL shortener is:
- **spiky** — a link can go viral, then go quiet. Serverless scales to the
  spike and to zero automatically.
- **read-heavy** — redirects vastly outnumber creates. A single-key `GetItem`
  on DynamoDB is the ideal primitive for that.
- **simple per request** — each request does a tiny amount of work, which is
  exactly Lambda's sweet spot.

---

## Request flows

### Creating a link — `POST /urls`
```
Client ──► API Gateway ──► CreateUrlFunction
                              │
                              │ 1. validate URL + optional custom code / expiry
                              │ 2. claim a short code:
                              │      PutItem(ConditionExpression=
                              │              attribute_not_exists(shortCode))
                              │    ├─ success  → code is ours
                              │    └─ conflict → retry with a new code (auto)
                              │ 3. emit "LinksCreated" metric
                              ▼
                         201 { shortUrl, shortCode, expiresAt, ... }
```
The conditional `PutItem` is the heart of **collision detection** — see
[Key design decisions](#1-collision-detection-is-the-databases-job).

### Following a link — `GET /{shortCode}` (the hot path)
```
Client ──► API Gateway ──► RedirectFunction
                              │
                              │ 1. GetItem(shortCode)           ← ~1 ms
                              │ 2. not found?  → 404
                              │ 3. expired?    → 410
                              │ 4. record analytics (best-effort):
                              │      • put click event (country, referrer, ts)
                              │      • atomic increment clickCount
                              │      • emit ClicksByCountry / ClicksByReferrer
                              ▼
                         301 Location: <longUrl>   (Cache-Control: 1 day)
```
Analytics is wrapped in try/except so **a failure to record a click never
breaks a redirect** — resolving the link is the user's actual goal.

### Reading analytics — `GET /urls/{shortCode}/analytics`
```
Client ──► API Gateway ──► GetAnalyticsFunction
                              │ 1. GetItem(shortCode) → total clickCount, metadata
                              │ 2. Query the Clicks table for this code
                              │ 3. aggregate in memory:
                              │      by country / by referrer / by day
                              ▼
                         200 { totalClicks, clicksByCountry, clicksByDay, ... }
```

---

## Data model

Two DynamoDB tables, both on-demand billed.

### `UrlsTable` — one row per short link
| Attribute | Type | Notes |
|---|---|---|
| `shortCode` **(PK)** | S | The code in the URL. Partition key → O(1) lookups. |
| `longUrl` | S | Redirect target. |
| `owner` | S | Who created it (`anonymous` if unset). |
| `createdAt` | S | ISO-8601 timestamp. |
| `clickCount` | N | Authoritative lifetime click total (atomically incremented). |
| `custom` | BOOL | Whether the code was user-supplied. |
| `expiresAt` | N | *(optional)* epoch seconds — used for the exact expiry check on read. |
| `ttl` | N | *(optional)* mirrors `expiresAt`; drives DynamoDB's background TTL delete. |

**GSI `owner-createdAt-index`**: partition `owner`, sort `createdAt`. Lets
"list my links, newest first" be an efficient `Query` instead of a table scan.

### `ClicksTable` — one row per click (analytics events)
| Attribute | Type | Notes |
|---|---|---|
| `shortCode` **(PK)** | S | Groups all clicks for a link together. |
| `clickId` **(SK)** | S | `"<iso-timestamp>#<uuid8>"` — chronological + collision-free. |
| `timestamp` | S | ISO-8601 of the click. |
| `date` | S | `YYYY-MM-DD` bucket for the daily time series. |
| `country` | S | ISO country code (from CloudFront) or `UNKNOWN`. |
| `referrer` | S | Referring host (e.g. `t.co`) or `direct`. |
| `userAgent` | S | Truncated UA string. |
| `ttl` | N | Epoch; DynamoDB deletes the row `ClickRetentionDays` after the click. |

**Why two tables?** They have opposite lifecycles and access patterns. `Urls`
rows are long-lived and read on every redirect; `Clicks` rows are
high-volume, append-only, and disposable after the retention window. Splitting
them keeps the hot redirect path's table small and lets clicks expire without
touching link metadata. The permanent `clickCount` counter lives on the
`Urls` row so totals survive even after individual click events are TTL'd away.

---

## Key design decisions

### 1. Collision detection is the *database's* job
A naive shortener does "check if code exists, then write it" — a
read-then-write race where two concurrent requests can both see "free" and
both write. Instead, every claim is a single conditional write:

```python
table.put_item(Item=..., ConditionExpression="attribute_not_exists(shortCode)")
```

DynamoDB evaluates the condition atomically, so exactly one writer wins even
under concurrency. The application never has to lock or coordinate; it just
retries with a fresh candidate if the write is rejected
(`shortcode.generate_unique_code`). After repeated collisions the candidate
length grows by a character, so the keyspace expands as the table fills.

### 2. Codes come from a CSPRNG, not a counter
Sequential IDs (1, 2, 3 → base62) are dense and predictable — anyone can
enumerate every link. We use `secrets.choice` (a cryptographically secure RNG)
over a 62-symbol alphabet, so codes are unguessable and the 62⁷ ≈ 3.5-trillion
keyspace keeps collisions astronomically rare at any realistic scale.

### 3. Metrics via Embedded Metric Format (EMF), not `PutMetricData`
Calling the CloudWatch API on the hot path would add latency and cost to every
redirect. Instead we write specially-structured JSON to stdout; CloudWatch
parses those log lines into real metrics asynchronously. Zero added request
latency, near-zero cost. See [`ANALYTICS.md`](ANALYTICS.md#how-metrics-are-emitted).

### 4. Shared code as a Lambda layer
All four functions need the same helpers (short codes, DynamoDB access,
metrics). Packaging them once as a layer (`layers/common`) keeps each
function's deployment package tiny and avoids copy-pasted logic. At runtime the
layer is mounted at `/opt/python`, so `import urlshortener_common` just works.

### 5. Two-tier click accounting
The lifetime total (`clickCount`) is an atomic counter on the URL row —
always correct, always cheap to read. The detailed breakdowns (country,
referrer, day) are computed from raw click events that expire after the
retention window. You keep long-term totals forever without paying to store
every raw event forever.

### 6. 301 (permanent) redirects
A `301` with `Cache-Control` lets browsers, crawlers, and CDNs cache the
mapping, so a viral link's repeat traffic is served without ever invoking
Lambda. The trade-off: you can't change a link's destination after clients
cache it — acceptable for a shortener, where a code maps to one URL for life.
(Switch to `302` in `responses.redirect` if you need mutable destinations.)

---

## Cost & scaling

**Scaling ceiling per component**

| Component | Scales by | Practical limit |
|---|---|---|
| API Gateway | Automatic | 10k rps default account quota (raise via support). |
| Lambda | Concurrent executions | 1,000 default concurrency (raise via support); each redirect is a few ms. |
| DynamoDB (reads) | Partitions | On-demand handles thousands of reads/s per partition; `shortCode` keys spread load evenly. |
| DynamoDB (writes) | Partitions | Click writes spread across many `shortCode` partitions; no single hot key. |

**Rough cost intuition** (us-east-1, on-demand, order-of-magnitude):
- DynamoDB on-demand: ~$1.25 per million writes, ~$0.25 per million reads.
- Lambda: ~$0.20 per million requests + tiny compute (arm64, 256 MB, few ms).
- API Gateway REST: ~$3.50 per million requests.

So ~1M redirects ≈ a few dollars, and **idle cost is essentially $0** because
nothing is provisioned. Caching 301s further cuts the Lambda/DynamoDB share of
repeat traffic.

---

## Security posture

- **HTTPS only** — API Gateway terminates TLS; there is no plaintext endpoint.
- **Unguessable codes** — CSPRNG generation (see decision #2).
- **Least-privilege IAM** — each function gets only the DynamoDB actions it
  needs, scoped to the specific tables, via SAM policy templates
  (`DynamoDBCrudPolicy`, `DynamoDBReadPolicy`, `CloudWatchPutMetricPolicy`).
- **Input validation** — only absolute `http(s)` URLs are accepted; custom
  codes are regex-restricted and reserved words are blocked.
- **Throttling** — API Gateway enforces per-stage burst/rate limits to blunt
  abuse.
- **Reserved routes** — codes like `urls`/`api` can't be issued, so a link can
  never shadow an API route.

**Not included (deliberately, to keep the scope resume-sized):** user
authentication, per-user rate limiting, and malicious-URL/safe-browsing
screening. These are called out as next steps in [`DEVELOPMENT.md`](DEVELOPMENT.md).

---

## Failure modes & how they're handled

| Failure | Behaviour |
|---|---|
| Short code already taken (auto) | Transparent retry with a new candidate; metric `ShortCodeCollision`. |
| Short code already taken (custom) | `409 Conflict` with a clear message. |
| Can't find a free code after N retries | `503` asking the client to retry (astronomically unlikely). |
| Unknown short code | `404 Not found`. |
| Expired link | `410 Gone` (exact check on read; TTL cleans up later). |
| Analytics write fails on redirect | Swallowed and logged; the `301` still succeeds. |
| Invalid input | `400` with a specific message (bad URL, bad custom code, bad expiry). |
| Redirect function erroring | CloudWatch alarm `url-shortener-redirect-errors-*` fires at ≥5 errors / 5 min. |
