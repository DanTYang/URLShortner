# Development Guide

How to work on this codebase locally: run the tests, run the API, understand
how the shared layer is wired, and add new functionality.

- [Setup](#setup)
- [Running the tests](#running-the-tests)
- [How imports work (the shared layer)](#how-imports-work-the-shared-layer)
- [Running the API locally](#running-the-api-locally)
- [Invoking a single function with a sample event](#invoking-a-single-function)
- [Project conventions](#project-conventions)
- [Adding a new endpoint](#adding-a-new-endpoint)
- [Ideas for next steps](#ideas-for-next-steps)

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r tests/requirements.txt
```

That's all you need for the tests (they mock AWS). For deploying and running
the API locally you also need the AWS SAM CLI and Docker — see
[`DEPLOYMENT.md`](DEPLOYMENT.md#prerequisites).

---

## Running the tests

```bash
make test          # or: pytest
```

The suite (in `tests/`) exercises the **real handler code** against a **mocked
DynamoDB** via [`moto`](https://github.com/getmoto/moto). No AWS account, no
network, no Docker. What's covered:

| File | Covers |
|---|---|
| `test_shortcode.py` | Base62 generation, custom-code validation, collision retry & keyspace growth. |
| `test_create_url.py` | Create happy path, custom codes, 409 collisions, validation, expiry persistence. |
| `test_redirect.py` | Redirects, 404/410, click recording, counter increments, best-effort analytics. |
| `test_analytics.py` | Analytics aggregation by facet, the list endpoint (scan + GSI paths). |

`tests/conftest.py` is where the magic lives:
- it sets the environment variables the code reads at import time,
- creates mocked DynamoDB tables that mirror `template.yaml`,
- re-points the application's cached table handles at the mocks, and
- provides `load_handler(name)` and `api_event(**kwargs)` helpers.

---

## How imports work (the shared layer)

In production, the shared code is a **Lambda layer**. Lambda unpacks a layer to
`/opt`, and `/opt/python` is on `sys.path`, so a function can simply:

```python
from urlshortener_common import dynamo, metrics, responses
```

The directory `layers/common/python/urlshortener_common/` is laid out to match
that runtime path exactly. Locally, `tests/conftest.py` adds
`layers/common/python` to `sys.path` so the same import works under pytest with
no packaging step.

Each function's entry module is `app.py` with a `handler(event, context)`
function — that's what `template.yaml` points `Handler: app.handler` at. Because
all four share the name `app`, the tests load them by path under unique names
(`load_handler("create_url")`), rather than a plain `import app`.

---

## Running the API locally

With Docker running:
```bash
make local        # sam local start-api  → http://127.0.0.1:3000
```
SAM spins up each function in a Lambda-like container behind a local API
Gateway emulator. Then hit it like the real thing:
```bash
curl -s -X POST http://127.0.0.1:3000/urls \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","customCode":"demo"}'

curl -si http://127.0.0.1:3000/demo | head -3
```

> `sam local` still talks to **real DynamoDB** by default (it only emulates
> Lambda + API Gateway). Point the functions at a local DynamoDB by setting the
> table env vars and running [DynamoDB Local](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBLocal.html),
> or just deploy a `dev` stack and test against that. For fast iteration on
> business logic, the mocked pytest suite is the quickest loop.

---

## Invoking a single function

Sample API Gateway events live in `events/`. Invoke one function in isolation:
```bash
sam build
sam local invoke CreateUrlFunction -e events/create_url.json
sam local invoke RedirectFunction  -e events/redirect.json
```

---

## Project conventions

- **Handlers stay thin.** HTTP parsing and validation live in `app.py`; all
  storage/logic lives in the shared layer. If a handler is doing DynamoDB work
  directly, that logic probably belongs in `dynamo.py`.
- **Configuration comes from the environment.** Never hard-code a table name or
  tunable — add it to `template.yaml` and read it in `config.py`.
- **Metrics for anything worth graphing.** Emit via `metrics.py` (EMF), not the
  CloudWatch API.
- **Analytics is best-effort** on the redirect path — never let it raise.
- **Least privilege.** When a function needs a new AWS action, add the narrowest
  SAM policy for it, scoped to the specific resource.

---

## Adding a new endpoint

Say you want `DELETE /urls/{shortCode}`:

1. **Create the function code** — `src/delete_url/app.py` with a
   `handler(event, context)`, plus an (empty) `requirements.txt` and
   `__init__.py`. Reuse the layer helpers.
2. **Add storage logic** if needed — e.g. a `dynamo.delete_url(short_code)`
   using a conditional delete.
3. **Register it in `template.yaml`** — a new `AWS::Serverless::Function` with a
   `DynamoDBCrudPolicy` and an `Api` event for `Path: /urls/{shortCode}`,
   `Method: delete`.
4. **Test it** — add `tests/test_delete_url.py` using the `aws` fixture and
   `load_handler("delete_url")`.
5. **Document it** — add a section to [`API.md`](API.md).

`sam build && sam deploy` ships it.

---

## Ideas for next steps

Natural extensions, roughly easiest → hardest, if you want to keep growing the
project:

- **Authentication** — protect `POST /urls` / `GET /urls` with API Gateway API
  keys or a Cognito authorizer, and scope listings to the authenticated user.
- **Per-user rate limiting** — API Gateway usage plans, or a token bucket in
  DynamoDB.
- **Malicious-URL screening** — check submissions against a safe-browsing list
  before shortening.
- **CloudFront in front** — enables real `CloudFront-Viewer-Country` geography
  and edge caching of redirects. (See [`ANALYTICS.md`](ANALYTICS.md#geography).)
- **Custom domain** — `short.yourdomain.com` via API Gateway custom domains +
  ACM certificate.
- **QR codes** — return a QR image for each short link.
- **Async analytics** — move click recording off the redirect path entirely
  via DynamoDB Streams or an SQS/EventBridge fan-out for extreme scale.
