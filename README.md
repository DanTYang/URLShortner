# Serverless URL Shortener

A production-shaped URL shortening service on AWS Lambda, API Gateway and
DynamoDB, defined end to end as infrastructure-as-code with AWS SAM.

Custom short codes with race-free collision detection, optional link expiry,
and per-link click analytics (counts, geography, referrers) surfaced through
CloudWatch metrics and a dependency-free browser dashboard.

---

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

| Method & path | Purpose |
|---|---|
| `POST /urls` | Create a short link — auto or custom code, optional expiry |
| `GET /{shortCode}` | Resolve, record a click, `301` to the target |
| `GET /urls/{shortCode}/analytics` | Click totals and breakdowns for one link |
| `GET /urls` | List links, optionally filtered by owner |

Full details in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Design decisions worth reading

**Collision detection belongs to the database.** The naive approach — check
whether a code exists, then write it — is a read-then-write race that no amount
of application code fixes. Every claim here is a single conditional write:

```python
table.put_item(Item=item, ConditionExpression="attribute_not_exists(shortCode)")
```

DynamoDB evaluates that atomically, so exactly one writer wins under any
concurrency and the loser simply retries with a fresh candidate. Uniqueness
becomes a property of the storage engine rather than something the application
has to coordinate. The same reasoning drives the atomic `UpdateExpression` used
for click counts.

**Codes come from a CSPRNG, not a counter.** Sequential IDs are dense and
enumerable. Since a short link is a bearer token — knowing the code is enough to
follow it — codes are drawn from `secrets` over a 62-symbol alphabet.

**Metrics ride the log stream.** Calling `PutMetricData` on the redirect path
would add a network round trip to every request. Instead the functions emit
CloudWatch Embedded Metric Format documents to stdout, which Lambda already
ships; CloudWatch parses them into real metrics asynchronously. Zero added
request latency.

**Analytics is best-effort, deliberately.** Resolving the link is the user's
goal; recording the click is ours. The click write is wrapped so a failure
loses the event rather than the redirect — an explicit decision about which
failure is worse, documented at the call site.

**Two-tier click accounting.** The lifetime total is an atomic counter on the
URL row and lives forever; the detailed breakdowns come from raw click events
that TTL out after 90 days. Long-term totals without paying to store every raw
event forever.

**Least privilege in IAM.** The two read-only functions get
`DynamoDBReadPolicy` rather than `Crud`, and the list function has no access to
the clicks table at all — so a bug in one bounds its own blast radius.

---

## Deploying

```bash
sam validate --lint
sam build
sam deploy --guided          # first run; answers are saved to samconfig.toml
```

The stack prints an `ApiBaseUrl` on completion:

```bash
API=https://xxxx.execute-api.us-east-1.amazonaws.com/dev

curl -s -X POST "$API/urls" -H 'Content-Type: application/json' \
  -d '{"url":"https://aws.amazon.com/lambda/","customCode":"lambda"}'

curl -si "$API/lambda" | head -3          # 301 + Location
curl -s "$API/urls/lambda/analytics"      # the click, recorded
```

`samconfig.toml` holds named `dev` / `staging` / `prod` configurations, so
replicating an environment is `sam deploy --config-env staging`.

Tear down with `make delete`. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## Repository layout

```
template.yaml            # the entire infrastructure, one file
samconfig.toml           # per-environment deploy settings
src/                     # one directory per Lambda
  create_url/  redirect/  get_analytics/  list_urls/
layers/common/           # shared code, deployed as a Lambda layer
  python/urlshortener_common/
    config.py  shortcode.py  dynamo.py  metrics.py  geo.py  responses.py
dashboard/index.html     # dependency-free browser client
events/                  # sample API Gateway events for local invocation
docs/                    # architecture, API reference, deployment, analytics
```

---

## Known limitations

Deliberate scope choices, not oversights. Each is something I would address
before this served real traffic:

- **No authentication.** The redirect endpoint is correctly public — that is
  the product. The other three are not, and `owner` is a client-supplied string,
  so it groups links rather than protecting them. Minimum fix is API Gateway
  API keys; the right fix is a Cognito authorizer with the owner read from the
  verified token rather than the request body.
- **Click recording is synchronous**, tripling DynamoDB operations on the
  busiest endpoint. Off the request path via DynamoDB Streams or SQS at volume.
- **Analytics aggregates up to 5,000 events per request**, and reports
  `truncated` when it hits that bound. Precomputed daily rollups are the real
  answer.
- **Geography depends on CloudFront** for the `CloudFront-Viewer-Country`
  header. Behind a bare API Gateway URL it degrades to `UNKNOWN`.
- **Cached 301s outlive short expiries.** A one-hour link stays in a browser
  cache for the full `max-age`. Capping `max-age` at the remaining TTL, or
  serving `302` for expiring links, resolves it.
- **No malicious-URL screening.** The failure that actually kills a shortener
  is the domain landing on a phishing blocklist, which breaks every link ever
  issued. A Safe Browsing check at creation is the highest-value addition here.

---

## License

MIT — see [`LICENSE`](LICENSE).
