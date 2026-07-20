# Serverless URL Shortener

A scalable URL shortening service built entirely on AWS serverless primitives:
**AWS Lambda**, **API Gateway**, and **DynamoDB**, deployed as
infrastructure‑as‑code with **AWS SAM**. It generates custom short codes with
collision detection, supports optional link expiration, and tracks click
analytics (counts, geography, referrers) which are surfaced through
**CloudWatch** metrics, a CloudWatch dashboard, and a small browser dashboard.

> **New to the project or coming back after a while?** Start with
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the "how it all fits
> together" picture, then use this README as the quick reference.

---

## What it does

| Capability | How it works |
|---|---|
| **Shorten a URL** | `POST /urls` stores a `shortCode → longUrl` mapping in DynamoDB and returns a short link. |
| **Custom codes** | Supply `customCode` (e.g. `spring-sale`); validated and claimed atomically. |
| **Collision detection** | Short codes are claimed with a DynamoDB *conditional write*, so two URLs can never share a code even under concurrent traffic. |
| **Expiring links** | Supply `expiresInDays`; the link 410s after it lapses and is auto‑deleted by DynamoDB TTL. |
| **Redirect** | `GET /{shortCode}` resolves the code and issues a cacheable `301` redirect. |
| **Analytics** | Every redirect records a click event (country, referrer, timestamp) and emits CloudWatch metrics. |
| **Dashboards** | A CloudWatch dashboard shows live traffic/latency/geography; a bundled HTML page shows per‑link analytics. |
| **One‑command deploy** | `sam build && sam deploy` provisions the entire stack; repeat with a different `--config-env` to replicate environments. |

---

## Architecture at a glance

```
                    ┌─────────────────────────────────────────────┐
   Client / browser │                 API Gateway                 │
   ───────────────► │  POST /urls   GET /{code}   GET /urls/...    │
                    └───────┬───────────┬──────────────┬──────────┘
                            │           │              │
                   ┌────────▼──┐  ┌─────▼──────┐  ┌────▼─────────┐
                   │ CreateUrl │  │  Redirect  │  │ GetAnalytics │   AWS Lambda
                   │  λ        │  │   λ        │  │  / ListUrls λ │  (Python 3.12)
                   └────┬──────┘  └──┬─────┬───┘  └──────┬───────┘
                        │            │     │             │
             conditional│      get   │     │ record      │ query
                   put   │      item │     │ click       │
                        ▼            ▼     ▼             ▼
                 ┌──────────────────────┐  ┌──────────────────────┐
                 │  DynamoDB: Urls      │  │  DynamoDB: Clicks     │
                 │  (shortCode → url)   │  │  (per‑click events)   │
                 └──────────────────────┘  └──────────────────────┘
                        │                          │
                        └──────── EMF metrics ─────┴────────► CloudWatch
                                                              (metrics + dashboard)
```

Full details, data model, and design trade‑offs live in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Repository layout

```
.
├── template.yaml            # SAM/CloudFormation — the entire infrastructure
├── samconfig.toml           # Per-environment deploy settings (dev/staging/prod)
├── Makefile                 # `make test`, `make deploy`, ... convenience targets
│
├── src/                     # One folder = one Lambda function
│   ├── create_url/app.py    #   POST /urls
│   ├── redirect/app.py      #   GET  /{shortCode}
│   ├── get_analytics/app.py #   GET  /urls/{shortCode}/analytics
│   └── list_urls/app.py     #   GET  /urls
│
├── layers/common/           # Shared code, deployed as a Lambda layer
│   └── python/urlshortener_common/
│       ├── config.py        #   env-driven configuration
│       ├── shortcode.py     #   base62 generation + collision retry
│       ├── dynamo.py        #   DynamoDB access layer
│       ├── metrics.py       #   CloudWatch metrics (EMF)
│       ├── geo.py           #   country / referrer extraction
│       └── responses.py     #   API Gateway response helpers
│
├── dashboard/index.html     # Self-contained browser analytics dashboard
├── events/                  # Sample API Gateway events for local testing
├── tests/                   # pytest suite (mocked AWS via moto)
└── docs/                    # Extended documentation (start here!)
    ├── ARCHITECTURE.md
    ├── API.md
    ├── DEPLOYMENT.md
    ├── ANALYTICS.md
    └── DEVELOPMENT.md
```

---

## Quick start

### 1. Prerequisites
- An AWS account and credentials (`aws configure`)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.12 and Docker (Docker only needed for `sam local`)

### 2. Deploy everything (one command, first time is guided)
```bash
sam build
sam deploy --guided        # answer the prompts once; saved to samconfig.toml
```
When it finishes, SAM prints the outputs — grab **`ApiBaseUrl`**:
```
Key                 ApiBaseUrl
Value               https://abc123.execute-api.us-east-1.amazonaws.com/dev
```

### 3. Create and use a short link
```bash
API=https://abc123.execute-api.us-east-1.amazonaws.com/dev

# Create
curl -s -X POST "$API/urls" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://aws.amazon.com/lambda/", "customCode": "lambda"}'
# → { "shortUrl": "https://.../dev/lambda", ... }

# Follow the short link (‑L follows the 301 redirect)
curl -sL "$API/lambda" -o /dev/null -w '%{http_code}\n'

# See analytics
curl -s "$API/urls/lambda/analytics" | jq
```

### 4. Open the dashboards
- **CloudWatch dashboard** — URL is in the stack outputs (`DashboardUrl`).
- **Browser dashboard** — open `dashboard/index.html`, paste your `ApiBaseUrl`.

Full API reference: [`docs/API.md`](docs/API.md).
Full deployment guide (multi‑env, teardown, CI): [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## Local development & testing

The test suite runs entirely offline — AWS is mocked with
[`moto`](https://github.com/getmoto/moto), so no account or network is needed.

```bash
make install     # pip install -r tests/requirements.txt
make test        # run the pytest suite
make validate    # lint the SAM template (needs sam or cfn-lint)
make local       # run the API locally on :3000 (needs Docker)
```

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for the full local workflow,
how the shared layer is wired for imports, and how to add a new endpoint.

---

## Configuration

Everything tunable is a CloudFormation parameter (in `template.yaml`), so the
same code deploys unchanged across environments:

| Parameter | Default | Meaning |
|---|---|---|
| `Environment` | `dev` | Namespaces every resource and the metrics namespace. |
| `ShortCodeLength` | `7` | Base62 characters per auto‑generated code (62⁷ ≈ 3.5 trillion). |
| `ClickRetentionDays` | `90` | How long raw click events live before TTL cleanup. |
| `MaxCollisionRetries` | `5` | Collision retries before returning an error. |

Override at deploy time, e.g.:
```bash
sam deploy --config-env prod \
  --parameter-overrides Environment=prod ShortCodeLength=8 ClickRetentionDays=365
```

---

## Cost & scaling notes

- **DynamoDB** uses on‑demand (`PAY_PER_REQUEST`) billing — no capacity to
  provision; it scales with traffic and costs nothing when idle.
- **Lambda + API Gateway** are pay‑per‑request with generous free tiers.
- **Redirects are cached** (`Cache-Control` + `301`), so repeat traffic to a
  popular link is largely served by browsers/CDNs, not Lambda.
- The redirect path is a single‑digit‑millisecond `GetItem` keyed on
  `shortCode`, so it scales horizontally without hot partitions.

For a back‑of‑the‑envelope cost model and the scaling ceiling of each
component, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#cost--scaling).

---

## License

MIT — see [`LICENSE`](LICENSE).
