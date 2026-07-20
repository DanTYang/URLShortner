"""Shared pytest fixtures.

These tests exercise the real handler code against *mocked* AWS services
(via ``moto``), so they run entirely offline and never touch a real account.

Two things have to happen before the application code is imported:

1. The environment variables the code reads at import time must be set.
2. The shared layer directory must be importable as ``urlshortener_common``.

We do both here, at module import time, so ``conftest.py`` is the single entry
point that makes ``import`` in the tests "just work".
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# --- Make the code importable exactly the way Lambda sees it ---------------
_ROOT = Path(__file__).resolve().parents[1]
# The layer is mounted at /opt/python in Lambda; locally we add its python/ dir.
sys.path.insert(0, str(_ROOT / "layers" / "common" / "python"))

# --- Configuration the code reads from the environment ---------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("URLS_TABLE", "url-shortener-urls-test")
os.environ.setdefault("CLICKS_TABLE", "url-shortener-clicks-test")
os.environ.setdefault("SHORT_CODE_LENGTH", "7")
os.environ.setdefault("MAX_COLLISION_RETRIES", "5")
os.environ.setdefault("CLICK_RETENTION_DAYS", "90")
os.environ.setdefault("METRICS_NAMESPACE", "URLShortener/test")
os.environ.setdefault("ENVIRONMENT", "test")


@pytest.fixture()
def aws(monkeypatch):
    """Spin up mocked DynamoDB tables that mirror ``template.yaml``.

    Yields the two ``boto3`` Table resources. Because the application modules
    cache their own table handles at import time, we re-point those module
    globals at the freshly-created mock tables for each test.
    """
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")

        urls = ddb.create_table(
            TableName=os.environ["URLS_TABLE"],
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "shortCode", "AttributeType": "S"},
                {"AttributeName": "owner", "AttributeType": "S"},
                {"AttributeName": "createdAt", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "shortCode", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner-createdAt-index",
                    "KeySchema": [
                        {"AttributeName": "owner", "KeyType": "HASH"},
                        {"AttributeName": "createdAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        clicks = ddb.create_table(
            TableName=os.environ["CLICKS_TABLE"],
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "shortCode", "AttributeType": "S"},
                {"AttributeName": "clickId", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "shortCode", "KeyType": "HASH"},
                {"AttributeName": "clickId", "KeyType": "RANGE"},
            ],
        )

        # Re-point the cached table handles inside the application module.
        from urlshortener_common import dynamo

        monkeypatch.setattr(dynamo, "_urls_table", urls)
        monkeypatch.setattr(dynamo, "_clicks_table", clicks)

        yield {"urls": urls, "clicks": clicks}


def load_handler(function_name: str):
    """Import a function's ``app.py`` under a unique module name.

    All four functions name their entry module ``app``, so a plain
    ``import app`` would collide. We load each by file path under a distinct
    name (``app_create_url`` etc.) so every handler is addressable.
    """
    module_name = f"app_{function_name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = _ROOT / "src" / function_name / "app.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def api_event(**kwargs):
    """Build a minimal API Gateway proxy event for tests."""
    event = {
        "headers": {"Host": "abc.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {"stage": "test", "identity": {"sourceIp": "1.2.3.4"}},
        "pathParameters": {},
        "queryStringParameters": {},
        "body": None,
    }
    event.update(kwargs)
    return event
