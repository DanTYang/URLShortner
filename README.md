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
| `GET /{shortCode}` | Resolve a code, record a click, return a `301`. |
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

Sequential identifiers encoded to base62 are dense and enumerable. A short link
is a bearer token, since knowing the code is sufficient to follow it, so codes
come from `secrets` over a 62-symbol alphabet. Seven characters give roughly
3.5 trillion combinations. Sustained collisions widen the code by one character.

### Metrics are emitted through the log stream

Calling `PutMetricData` on the redirect path would add a network round trip to
every request. The functions instead write CloudWatch Embedded Metric Format
documents to stdout, which Lambda already ships to CloudWatch Logs. CloudWatch
extracts them into metrics asynchronously, adding no request latency.

### Analytics recording is best-effort

Resolving a link is the request's purpose; recording the click is a side
effect. The click write is wrapped so that a failure loses the event rather
than the redirect. The trade-off is documented at the call site.

### Click accounting is two-tier

Lifetime totals come from an atomic counter on the URL row and persist
indefinitely. Country, referrer and daily breakdowns are computed from raw
click events, which expire after 90 days via DynamoDB TTL. Long-term totals are
retained without storing every raw event permanently.

### IAM follows least privilege

The two read-only functions receive `DynamoDBReadPolicy` rather than
`DynamoDBCrudPolicy`. `ListUrlsFunction` has no access to the clicks table at
all. A fault in any one function is bounded by the permissions of its own role.

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
distributes evenly across partitions and avoids hot keys. `301` responses carry
a `Cache-Control` header, so repeat traffic to a popular link is served by
browsers and CDNs without invoking Lambda.

Approximate order of magnitude in `us-east-1`: one million redirects costs a few
dollars, dominated by API Gateway request pricing.

## Limitations

The following are known and unaddressed in the current scope.

**No authentication.** The redirect endpoint is intentionally public. The other
three are not protected, and `owner` is a client-supplied string, so it groups
links rather than restricting access to them. API Gateway API keys would be the
minimum fix; a Cognito authorizer reading the owner from a verified token is the
correct one.

**Synchronous click recording.** Each redirect performs one read and two writes,
tripling DynamoDB operations on the highest-traffic endpoint. Moving the write
off the request path via DynamoDB Streams or SQS would remove that cost.

**Bounded analytics aggregation.** The analytics endpoint reads at most 5,000
click events per request and reports `truncated` when that bound is reached.
Precomputed daily rollups would remove the bound.

**Geography depends on CloudFront.** Country is read from the
`CloudFront-Viewer-Country` header. Behind a bare API Gateway URL the value
degrades to `UNKNOWN`.

**Cached redirects outlive short expiries.** A `301` is cached for the duration
of its `Cache-Control` header, so a link expiring sooner than that continues to
resolve from cache. Capping `max-age` at the remaining TTL, or serving `302`
for expiring links, resolves it.

**No destination screening.** Submitted URLs are validated for scheme and
length but not checked against a malicious-URL list. A shortener domain that
lands on a phishing blocklist breaks every link it has issued.

## License

MIT. See [`LICENSE`](LICENSE).
