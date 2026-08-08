"""CloudWatch metrics via the Embedded Metric Format (EMF).

Calling ``PutMetricData`` on the redirect path would add a network round trip
to every request, cost per call, and introduce another failure mode while a
user waits. Instead this module writes specially-structured JSON to stdout,
which Lambda already ships to CloudWatch Logs. CloudWatch parses those
documents asynchronously into real metrics — graphable and alarmable, for the
cost of a write to stdout and no added request latency.

Three details are load-bearing, and each fails *silently* if wrong: the
timestamp must be epoch milliseconds, every declared dimension must also appear
as a top-level key, and the document must occupy exactly one line.

Spec: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
"""

import json
import sys
import time

from . import config


def emit(metric_name, value=1.0, *, unit="Count", dimensions=None, extra=None):
    """Emit a single CloudWatch metric as an EMF log line.

    Parameters
    ----------
    metric_name:
        e.g. ``"Redirects"`` or ``"ClicksByCountry"``.
    value:
        The metric value; defaults to 1.
    unit:
        A CloudWatch unit — ``"Count"``, ``"Milliseconds"``, ``"Bytes"``, ...
    dimensions:
        Facets to slice by, e.g. ``{"Country": "US"}``.

        Keep cardinality low. Every distinct dimension *value* becomes a
        separately-billed custom metric: ``{"Country": ...}`` is ~200 metrics,
        whereas ``{"ShortCode": ...}`` would be one per link, forever.
    extra:
        Additional non-metric fields, included in the structured log for
        querying with CloudWatch Logs Insights but not billed as metrics.
    """
    timestamp = int(time.time() * 1000)
    dims = dimensions or {}
    document = {
        "_aws": {
            "Timestamp": timestamp,
            "CloudWatchMetrics": [
                {
                    "Namespace": config.METRICS_NAMESPACE,
                    # A list of dimension *sets*, hence the double nesting.
                    "Dimensions": [list(dims.keys())] if dims else [[]],
                    "Metrics": [{"Name": metric_name, "Unit": unit}],
                }
            ],
        },
        metric_name: value,
    }
    # Declared dimensions must also exist at the top level, or CloudWatch
    # discards the document without an error.
    document.update(dims)
    if extra:
        document.update(extra)

    # One document per line: EMF is parsed per log line, so an embedded newline
    # splits the document and both halves are dropped.
    sys.stdout.write(json.dumps(document) + "\n")


def count(metric_name, dimensions=None):
    """Emit a count of 1 for ``metric_name``."""
    emit(metric_name, 1.0, unit="Count", dimensions=dimensions)


def timing(metric_name, milliseconds):
    """Emit a duration metric in milliseconds."""
    emit(metric_name, milliseconds, unit="Milliseconds")
