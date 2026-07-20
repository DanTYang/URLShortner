"""Short-code generation and validation.

A "short code" is the trailing path segment of a short link
(``https://.../{shortCode}``). Codes are drawn from a 62-symbol alphabet so
they are compact and URL-safe.

Two things matter for correctness:

1. **Uniqueness.** Two different long URLs must never share a code. We get this
   from DynamoDB, not from the generator: the write is a conditional
   ``PutItem`` that only succeeds if the code does not already exist (see
   ``dynamo.put_url``). This module's job is only to *propose* candidate codes;
   :func:`generate_unique_code` retries with a fresh candidate whenever the
   conditional write reports a collision.

2. **Unpredictability.** Codes are generated with :mod:`secrets` (a CSPRNG)
   rather than :mod:`random`, so an attacker cannot trivially enumerate or
   guess other users' links.
"""

import re
import secrets
from typing import Callable

from . import config

# Custom codes users may supply themselves. Keep them URL-safe and a sensible
# length; hyphen and underscore are allowed for readability (e.g. "black-friday").
_CUSTOM_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

# Codes we never hand out because they collide with real API routes or read
# badly. The redirect route is ``GET /{shortCode}`` so a code of "urls" would
# shadow the ``/urls`` collection endpoint.
_RESERVED_CODES = {"urls", "url", "api", "health", "admin", "static", "favicon"}


def generate_candidate(length: int | None = None) -> str:
    """Return a single random base62 code of the configured length."""
    n = length or config.SHORT_CODE_LENGTH
    alphabet = config.BASE62_ALPHABET
    return "".join(secrets.choice(alphabet) for _ in range(n))


def is_valid_custom_code(code: str) -> bool:
    """True if ``code`` is an acceptable user-supplied custom code."""
    if not code or code.lower() in _RESERVED_CODES:
        return False
    return bool(_CUSTOM_CODE_RE.match(code))


def is_reserved(code: str) -> bool:
    """True if ``code`` is reserved and must not be issued."""
    return code.lower() in _RESERVED_CODES


def generate_unique_code(
    try_claim: Callable[[str], bool],
    *,
    length: int | None = None,
    max_retries: int | None = None,
) -> str:
    """Generate a code and claim it, retrying on collision.

    Parameters
    ----------
    try_claim:
        Callback that attempts to atomically reserve a candidate code. It must
        return ``True`` if the code was successfully claimed and ``False`` if
        the code already existed (a collision). This is where DynamoDB's
        conditional write plugs in.
    length:
        Override the configured code length (used to grow codes if a run of
        collisions suggests the keyspace is filling up).
    max_retries:
        How many collisions to tolerate before raising.

    Returns
    -------
    The successfully claimed short code.

    Raises
    ------
    ShortCodeCollisionError
        If a unique code could not be claimed within ``max_retries`` attempts.
    """
    retries = config.MAX_COLLISION_RETRIES if max_retries is None else max_retries
    base_len = length or config.SHORT_CODE_LENGTH

    for attempt in range(retries + 1):
        # After repeated collisions, widen the keyspace by growing the code by
        # one character. This makes sustained collisions vanishingly unlikely
        # even as the table fills up.
        candidate_len = base_len + (attempt // 2)
        candidate = generate_candidate(candidate_len)
        if is_reserved(candidate):
            continue
        if try_claim(candidate):
            return candidate

    raise ShortCodeCollisionError(
        f"Could not generate a unique short code after {retries + 1} attempts."
    )


class ShortCodeCollisionError(Exception):
    """Raised when a unique short code cannot be generated."""
