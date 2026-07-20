"""Runtime configuration, read once from environment variables.

Every value here is injected by ``template.yaml`` (see the ``Environment``
block of the Globals / function definitions). Reading configuration from the
environment — rather than hard-coding it — is what lets the *same* code run
unchanged across dev / staging / prod stacks.
"""

import os


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable, tolerating blank/unset values."""
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# DynamoDB table names (physical names created by CloudFormation).
URLS_TABLE = os.environ.get("URLS_TABLE", "url-shortener-urls-dev")
CLICKS_TABLE = os.environ.get("CLICKS_TABLE", "url-shortener-clicks-dev")

# Short-code behaviour.
SHORT_CODE_LENGTH = _int_env("SHORT_CODE_LENGTH", 7)
MAX_COLLISION_RETRIES = _int_env("MAX_COLLISION_RETRIES", 5)

# Analytics retention (how long individual click rows live before TTL cleanup).
CLICK_RETENTION_DAYS = _int_env("CLICK_RETENTION_DAYS", 90)

# CloudWatch custom-metric namespace, e.g. "URLShortener/prod".
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "URLShortener/dev")

# Which environment we are running in (dev / staging / prod).
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

# The base62 alphabet used for short codes. Digits + upper + lower gives 62
# symbols; it deliberately excludes URL-unsafe characters so codes never need
# escaping.
BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
