"""Shared helpers for the URL shortener Lambda functions.

This package is deployed as a Lambda *layer* (see ``template.yaml``). At
runtime the layer is unpacked to ``/opt/python`` which is on ``sys.path``, so
every function can simply ``from urlshortener_common import ...``.

Modules
-------
config      Environment-driven configuration (table names, code length, ...).
shortcode   Base62 short-code generation with DynamoDB collision detection.
dynamo      Thin DynamoDB access layer for URL rows and click events.
metrics     CloudWatch metric emission via the Embedded Metric Format (EMF).
geo         Extracts country / referrer / client info from an API GW event.
responses   Helpers for building API Gateway proxy responses.
"""

from . import config, dynamo, geo, metrics, responses, shortcode  # noqa: F401

__all__ = ["config", "dynamo", "geo", "metrics", "responses", "shortcode"]
