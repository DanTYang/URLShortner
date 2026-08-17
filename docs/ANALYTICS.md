# Analytics & Observability

This is the "analytics dashboard tracking click counts, geographic data, and
referrer sources using CloudWatch metrics" part of the project, explained end
to end.

- [Two views of the same data](#two-views-of-the-same-data)
- [What gets recorded on each click](#what-gets-recorded-on-each-click)
- [Geography](#geography)
- [Referrers](#referrers)
- [How metrics are emitted](#how-metrics-are-emitted)
- [The metrics catalogue](#the-metrics-catalogue)
- [The CloudWatch dashboard](#the-cloudwatch-dashboard)
- [The browser dashboard](#the-browser-dashboard)
- [Querying raw events with Logs Insights](#querying-raw-events)

---

## Two views of the same data

Click data is surfaced two complementary ways:

| | CloudWatch metrics/dashboard | Per-link analytics API + browser UI |
|---|---|---|
| **Source** | Custom metrics emitted as logs (EMF) | Raw click rows in `ClicksTable` |
| **Best for** | Fleet-wide, real-time operational view (totals, geography trend, errors, latency) | Deep dive on a *single* link |
| **Granularity** | Aggregated by metric + dimension | Individual events, arbitrary aggregation |
| **Retention** | CloudWatch metric retention (15 months, downsampled) | `ClickRetentionDays` (default 90) |

They come from the same redirect handler: on each click it both **emits
metrics** and **writes a raw event**.

---

## What gets recorded on each click

When `GET /{shortCode}` succeeds, `redirect/app.py` calls
`_record_analytics`, which:

1. Extracts facets from the request (see below).
2. Writes one row to `ClicksTable` (`dynamo.record_click`) with `country`,
   `referrer`, `userAgent`, `timestamp`, and a `date` bucket.
3. Atomically increments `clickCount` on the `UrlsTable` row.
4. Emits CloudWatch metrics `Redirects`, `RedirectHit`,
   `ClicksByCountry{Country=…}`, and `ClicksByReferrer{Referrer=…}`.

All of this is wrapped in try/except: **if analytics fails, the redirect still
succeeds.** A lost click event is strictly better than a failed redirect.

---

## Geography

Country is determined **without any GeoIP database or third-party service**.
When the API sits behind CloudFront, AWS resolves the client IP to a country
and injects a `CloudFront-Viewer-Country` header (an ISO-3166 alpha-2 code like
`US`, `GB`, `DE`). `geo.get_country` reads it, falling back to:

1. `CloudFront-Viewer-Country` (production, behind CloudFront),
2. `X-Country` (an override, handy for testing),
3. `"UNKNOWN"` otherwise.

> Behind a **bare API Gateway URL** there is no CloudFront header, so country
> records as `UNKNOWN`. To get real geography, front the API with CloudFront
> (and enable the viewer-country header). This is a deliberate, documented
> trade-off — it keeps the base stack free of extra moving parts while making
> real geo a single infrastructure addition.

---

## Referrers

`geo.get_referrer` reads the `Referer` header (yes, misspelled — a 1996 HTTP
typo we're all stuck with) and keeps **only the host**, e.g. `t.co` or
`news.ycombinator.com`. Keeping just the host:
- groups traffic cleanly by source, and
- avoids retaining full referring URLs, which can carry query strings with
  personal data.

No referrer → `"direct"` (someone typed the link or clicked it from an app that
sends no referrer).

---

## How metrics are emitted

Metrics use the **CloudWatch Embedded Metric Format (EMF)**. Instead of calling
the `PutMetricData` API — which would add latency and cost to every redirect —
`metrics.py` writes a structured JSON line to stdout:

```json
{
  "_aws": {
    "Timestamp": 1793664000000,
    "CloudWatchMetrics": [
      {
        "Namespace": "URLShortener/dev",
        "Dimensions": [["Country"]],
        "Metrics": [{ "Name": "ClicksByCountry", "Unit": "Count" }]
      }
    ]
  },
  "Country": "US",
  "ClicksByCountry": 1
}
```

CloudWatch automatically parses these lines out of the Lambda log stream and
materialises them as real metrics — asynchronously, off the request path. The
result: **rich custom metrics with zero added request latency and negligible
cost.**

---

## The metrics catalogue

All under namespace `URLShortener/<env>`:

| Metric | Dimensions | Emitted when |
|---|---|---|
| `LinksCreated` | — | A link is created. |
| `Redirects` | — | Any successful redirect. |
| `RedirectHit` | — | A redirect resolved to a target (302). |
| `RedirectNotFound` | — | Redirect for an unknown code (404). |
| `RedirectExpired` | — | Redirect for an expired link (410). |
| `ClicksByCountry` | `Country` | Per successful redirect, sliced by country. |
| `ClicksByReferrer` | `Referrer` | Per successful redirect, sliced by referring host. |
| `ShortCodeCollision` | — | A short-code claim hit an existing code. |

AWS also publishes standard `AWS/Lambda` metrics (Duration, Errors, Throttles)
and `AWS/ApiGateway` metrics for free — the dashboard uses these for latency
and error panels.

---

## The CloudWatch dashboard

`template.yaml` defines an `AWS::CloudWatch::Dashboard` named
`url-shortener-<env>`. Open it from the `DashboardUrl` stack output. Panels:

1. **Links created vs. redirects** — headline traffic.
2. **Redirect outcomes** — hits vs. 404s vs. 410s (stacked).
3. **Lambda duration (p50/p99)** — latency of the redirect and create functions.
4. **Errors & collisions** — code collisions plus Lambda errors.
5. **Clicks by country** — the geographic breakdown over time.

Because the dashboard is defined in the template, it's created and versioned
alongside the rest of the infrastructure — no click-ops.

The template also defines an alarm, `url-shortener-redirect-errors-<env>`,
that fires when the redirect function logs ≥5 errors in 5 minutes. Wire it to
an SNS topic to get paged.

---

## The browser dashboard

`dashboard/index.html` is a single, dependency-free page for exploring a
**single link's** analytics visually. Paste your `ApiBaseUrl`, then you can:
- create links,
- list them,
- and view a link's total clicks, a daily time-series line chart, and
  bar charts of clicks by country and by referrer.

It calls only the public API (`POST /urls`, `GET /urls`,
`GET /urls/{code}/analytics`) — there's no backend of its own. Host it on S3 +
CloudFront for a shareable dashboard, or just open the file locally. The chart
rendering is hand-written SVG so the file is fully self-contained.

---

## Querying raw events

Because EMF metrics are also structured logs, you can slice the raw data with
**CloudWatch Logs Insights** on the redirect function's log group. Example —
top referrers in the last day:

```
fields Referrer
| filter ispresent(ClicksByReferrer)
| stats count(*) as clicks by Referrer
| sort clicks desc
| limit 20
```

Or query the `ClicksTable` directly in DynamoDB for a given `shortCode` to see
individual events (that's exactly what `GET /urls/{code}/analytics` does).
