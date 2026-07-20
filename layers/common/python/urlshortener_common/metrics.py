"""CloudWatch metrics via the Embedded Metric Format (EMF).

Rather than calling the ``PutMetricData`` API on the hot path (which adds
latency and cost to every request), we emit metrics as specially-structured
JSON log lines. CloudWatch automatically parses these EMF documents out of the
Lambda log stream and turns them into real metrics — no extra API call, no
added request latency.

Reference: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
"""

import json
import sys
import time
from typing import Optional

from . import config


def emit(
    metric_name: str,
    value: float = 1.0,
    *,
    unit: str = "Count",
    dimensions: Optional[dict[str, str]] = None,
    extra: Optional[dict[str, object]] = None,
) -> None:
    """Emit a single CloudWatch metric as an EMF log line.

    Parameters
    ----------
    metric_name:
        e.g. ``"Redirects"`` or ``"ClicksByCountry"``.
    value:
        The metric value (defaults to 1, i.e. "count one occurrence").
    unit:
        A CloudWatch unit ("Count", "Milliseconds", ...).
    dimensions:
        Optional dimension map, e.g. ``{"Country": "US"}``. Dimensions let you
        slice the same metric (clicks) by facet (country) in the dashboard.
    extra:
        Additional non-metric fields to include in the structured log for
        debugging / querying with CloudWatch Logs Insights.
    """
    dims = dimensions or {}

    document: dict[str, object] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": config.METRICS_NAMESPACE,
                    "Dimensions": [list(dims.keys())] if dims else [[]],
                    "Metrics": [{"Name": metric_name, "Unit": unit}],
                }
            ],
        },
        metric_name: value,
    }
    document.update(dims)
    if extra:
        document.update(extra)

    # EMF is picked up from stdout in the Lambda log stream.
    sys.stdout.write(json.dumps(document) + "\n")


def count(metric_name: str, dimensions: Optional[dict[str, str]] = None) -> None:
    """Convenience wrapper: emit a count of 1 for ``metric_name``."""
    emit(metric_name, 1.0, unit="Count", dimensions=dimensions)


def timing(metric_name: str, milliseconds: float) -> None:
    """Convenience wrapper: emit a duration metric in milliseconds."""
    emit(metric_name, milliseconds, unit="Milliseconds")
