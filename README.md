# Serverless URL Shortener

A URL shortening service built on AWS Lambda, API Gateway and DynamoDB, defined
end to end as infrastructure-as-code with AWS SAM.

Supports custom short codes, race-free collision detection, optional link
expiry, and per-link click analytics covering counts, geography and referrers.
Analytics are surfaced through CloudWatch metrics and a dependency-free browser
dashboard.

## Endpoints

| Method and path | Purpose |
|---|---|
| `POST /urls` | Create a short link. Auto-generated or custom code, optional expiry. |
| `GET /{shortCode}` | Resolve a code, record a click, return a `302`. |
| `GET /urls/{shortCode}/analytics` | Click totals and breakdowns for one link. |
| `GET /urls` | List links, optionally filtered by owner. |

## Architecture

```
                    ┌─────────────────────────────────────────────┐
   Client / browser │                 API Gateway                 │
   ───────────────► │  POST /urls   GET /{code}   GET /urls/...   │
                    └───────┬───────────┬──────────────┬──────────┘
                            │           │              │
                   ┌────────▼──┐  ┌─────▼──────┐  ┌────▼──────────┐
                   │ CreateUrl │  │  Redirect  │  │ GetAnalytics  │  AWS Lambda
                   │     λ     │  │     λ      │  │  / ListUrls λ │  (Python 3.12,
                   └────┬──────┘  └──┬─────┬───┘  └──────┬────────┘   arm64)
                        │            │     │             │
             conditional│      get   │     │ record      │ query
                    put │      item  │     │ click       │
                        ▼            ▼     ▼             ▼
                 ┌──────────────────────┐  ┌──────────────────────┐
                 │  DynamoDB: Urls      │  │  DynamoDB: Clicks    │
                 │  (shortCode → url)   │  │  (per-click events)  │
                 └──────────────────────┘  └──────────────────────┘
                        │                          │
                        └──────── EMF metrics ─────┴────────► CloudWatch
```

Four single-purpose Lambda functions share one layer containing the storage,
metrics and request-parsing helpers. Two DynamoDB tables hold link mappings and
raw click events. Full detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### Data model

`UrlsTable` is partitioned on `shortCode`, so redirect lookups are single-key
`GetItem` calls. A global secondary index on `owner` and `createdAt` serves
per-owner listings without a table scan.

`ClicksTable` is partitioned on `shortCode` with a sort key of
`<iso-timestamp>#<uuid8>`, so all clicks for a link are stored together in
chronological order and can be range-queried directly.

## Design decisions

### Collision detection is delegated to the database

Checking whether a code exists and then writing it is a read-then-write race:
two concurrent requests can both observe the code as free and both write. Every
claim is instead a single conditional write.

```python
table.put_item(Item=item, ConditionExpression="attribute_not_exists(shortCode)")
```

DynamoDB evaluates the condition atomically, so exactly one writer succeeds
under any level of concurrency. The losing request retries with a fresh
candidate. Click counters use an atomic `UpdateExpression` for the same reason.

### Short codes are drawn from a CSPRNG

Codes are randomly generated base62 strings, not encoded counters. Sequential
identifiers are dense and enumerable, and a short link is a bearer token since
knowing the code is sufficient to follow it. Codes are drawn from `secrets`, a
CSPRNG, over a 62-symbol alphabet. Seven characters give roughly 3.5 trillion
combinations, and sustained collisions widen the code by one character.

### Metrics are emitted through the log stream

Calling `PutMetricData` on the redirect path would add a network round trip to
every request. The functions instead write CloudWatch Embedded Metric Format
documents to stdout, which Lambda already ships to CloudWatch Logs. CloudWatch
extracts them into metrics asynchronously, adding no request latency.

### Analytics recording is best-effort

Resolving a link is the request's purpose; recording the click is a side
effect. The click write is wrapped so that a failure loses the event rather
than the redirect. The trade-off is documented at the call site.

### Redirects are cached briefly, not permanently

Redirects return `302` with a one-minute `Cache-Control` window, capped at the
link's remaining lifetime. A cached response cannot be revoked, so that window
is the worst case for how long an expired link keeps resolving, and the same
bound will apply to deletion once that endpoint exists.

`301` would allow longer caching and fewer invocations, but it asserts
permanence, and some intermediaries cache a `301` indefinitely regardless of
`Cache-Control`. Since every link here can be deleted or expire, that claim
would be false.

The cost of the short window is small because browser caches are per-client and
do nothing for distinct visitors: a thousand people clicking one link is a
thousand cache misses at any `max-age`. Only repeat clicks from the same
browser are absorbed. A CDN in front would change this, since a shared cache
serves every visitor from one fetch.

### Click accounting is two-tier

Lifetime totals come from an atomic counter on the URL row and persist
indefinitely. Country, referrer and daily breakdowns are computed from raw
click events, which expire after 90 days via DynamoDB TTL. Long-term totals are
retained without storing every raw event permanently.

### Permissions are scoped per function

Each function declares its own SAM policy templates, which expand into an IAM
role scoped to the named tables rather than a shared role or a wildcard policy.
The two read-only functions receive `DynamoDBReadPolicy` rather than
`DynamoDBCrudPolicy`, and `ListUrlsFunction` has no access to the clicks table
at all. A fault in any one function is bounded by the permissions of its own
role.

## Deploying

Requires the AWS SAM CLI and configured AWS credentials.

```bash
sam validate --lint
sam build
sam deploy --guided     # first run only; answers are saved to samconfig.toml
```

The stack outputs an `ApiBaseUrl` on completion.

```bash
API=https://xxxx.execute-api.us-east-1.amazonaws.com/dev

curl -s -X POST "$API/urls" -H 'Content-Type: application/json' \
  -d '{"url":"https://aws.amazon.com/lambda/","customCode":"lambda"}'

curl -si "$API/lambda" | head -3
curl -s "$API/urls/lambda/analytics"
```

`samconfig.toml` holds named `dev`, `staging` and `prod` configurations, so
replicating an environment is `sam deploy --config-env staging`. Teardown is
`make delete`. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Configuration

Every tunable value is a CloudFormation parameter, so the same artifact deploys
unchanged across environments.

| Parameter | Default | Effect |
|---|---|---|
| `Environment` | `dev` | Namespaces all resources and the metrics namespace |
| `ShortCodeLength` | `7` | Base62 characters per generated code |
| `ClickRetentionDays` | `90` | Lifetime of raw click events before TTL deletion |
| `MaxCollisionRetries` | `5` | Collision retries before returning `503` |

## Repository layout

```
template.yaml            complete infrastructure definition
samconfig.toml           per-environment deploy settings
src/                     one directory per Lambda function
  create_url/  redirect/  get_analytics/  list_urls/
layers/common/           shared code, deployed as a Lambda layer
  python/urlshortener_common/
    config.py  shortcode.py  dynamo.py  metrics.py  geo.py  responses.py
dashboard/index.html     single-file browser client, no build step
events/                  sample API Gateway events for local invocation
docs/                    architecture, API reference, deployment, analytics
```

## Cost and scaling

DynamoDB uses on-demand billing, so capacity scales with traffic and idle cost
is negligible. Redirects are single-key lookups keyed on a random code, which
distributes evenly across partitions and avoids hot keys. Redirects carry a
short `Cache-Control` window, which absorbs repeat clicks from the same browser
but not traffic from distinct visitors.

Approximate order of magnitude in `us-east-1`: one million redirects costs a few
dollars, dominated by API Gateway request pricing.

## Roadmap

Planned work, in dependency order.

**User accounts via Google.** A Cognito user pool federating to Google, with a
Cognito authorizer on the three non-redirect routes. `owner` is then read from
the verified token rather than the request body, which turns it from a grouping
label into an access control. The redirect endpoint stays public. Listing
becomes scoped to the authenticated caller and the unfiltered scan path is
removed.

**Per-user link quota.** A cap of 50 to 100 simultaneously live links per user,
counted at creation time by querying the owner index and filtering out expired
rows. Returns `429` with the current usage when the cap is reached.

**Link deletion.** `DELETE /urls/{shortCode}` using a conditional delete so a
caller can only remove their own links. Frees a quota slot immediately.

**Hour-granularity lifetimes.** `expiresInHours` from 1 to 168, replacing the
current day-granularity field and capping link lifetime at seven days.

**Destination screening.** Checking submitted URLs against a malicious-URL list
such as Google Safe Browsing before shortening. A shortener domain that lands on
a phishing blocklist breaks every link it has ever issued, so this protects the
one thing that cannot be recovered.

**CloudFront distribution.** Fronting the API would supply real
`CloudFront-Viewer-Country` values, and a shared edge cache would absorb repeat
traffic from distinct visitors rather than only from the same browser.

## Limitations

Known and accepted in the current scope.

**Synchronous click recording.** Each redirect performs one read and two writes,
tripling DynamoDB operations on the highest-traffic endpoint. Moving the write
off the request path via DynamoDB Streams or SQS would remove that cost.

**Bounded analytics aggregation.** The analytics endpoint reads at most 5,000
click events per request and reports `truncated` when that bound is reached.
Precomputed daily rollups would remove the bound.

**Geography degrades without CloudFront.** Country is read from the
`CloudFront-Viewer-Country` header. Behind a bare API Gateway URL the value
falls back to `UNKNOWN`.

## License

MIT. See [`LICENSE`](LICENSE).
