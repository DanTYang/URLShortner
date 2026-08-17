"""Short-code generation and validation.

A short code is the trailing path segment of a short link
(``https://.../{shortCode}``). Codes are drawn from a 62-symbol alphabet so
they are compact and URL-safe without escaping.

Two properties matter, and only one of them is this module's job.

**Uniqueness is the database's job.** A check-then-write ("is this code free?
then take it") is a race: two concurrent requests can both observe "free" and
both write, and no amount of application code fixes that. Instead the claim is
a single conditional PutItem (see :func:`dynamo.put_url`), which DynamoDB
evaluates atomically. This module only *proposes* candidates and retries when
the caller's claim callback reports a collision.

**Unpredictability is this module's job.** Codes come from :mod:`secrets`, a
CSPRNG, rather than :mod:`random`. A Mersenne Twister's state is recoverable
from its output, and since a short link is a bearer token — knowing the code is
sufficient to read the target — predictable codes would let an attacker
enumerate every link in the system.
"""

import re
import secrets

from . import config

# User-supplied codes: URL-safe characters only, sensible length bounds.
# Hyphen and underscore are allowed for readability (e.g. "black-friday").
_CUSTOM_CODE_RE = re.compile(r"[A-Za-z0-9_-]{3,64}")

# Never issued, because the redirect route is `GET /{shortCode}` at the API
# root — a code of "urls" would contest the /urls collection endpoint.
_RESERVED_CODES = {"urls", "url", "api", "health", "admin", "static", "favicon"}


def generate_candidate(length=None):
    """Return one random base62 code of ``length`` characters.

    Defaults to ``config.SHORT_CODE_LENGTH``. Seven base62 characters give
    62**7 (~3.5 trillion) possible codes.
    """
    candidate = ""
    for _ in range(length or config.SHORT_CODE_LENGTH):
        candidate += secrets.choice(config.BASE62_ALPHABET)
    return candidate


def is_valid_custom_code(code):
    """True if ``code`` is acceptable as a user-supplied custom code."""
    if code is None or code == "":
        return False
    if is_reserved(code):
        return False
    return bool(_CUSTOM_CODE_RE.fullmatch(code))


def is_reserved(code):
    """True if ``code`` is reserved and must never be issued."""
    return code.lower() in _RESERVED_CODES


def generate_unique_code(try_claim, *, length=None, max_retries=None):
    """Generate a code and claim it, retrying on collision.

    Parameters
    ----------
    try_claim:
        Callback invoked with a candidate code. Must return True if the code
        was atomically reserved and False if it already existed. In production
        this is a DynamoDB conditional write. This module neither knows nor
        cares what backs it, which is what lets the real uniqueness guarantee
        live in the storage engine rather than here.
    length:
        Starting code length. Defaults to ``config.SHORT_CODE_LENGTH``.
    max_retries:
        Collisions tolerated before giving up. Defaults to
        ``config.MAX_COLLISION_RETRIES``.

    Returns
    -------
    The claimed short code.

    Raises
    ------
    ShortCodeCollisionError
        If no unique code could be claimed within the retry budget.
    """
    base_length = length or config.SHORT_CODE_LENGTH
    retries = max_retries if max_retries is not None else config.MAX_COLLISION_RETRIES

    for attempt in range(retries + 1):
        # Grow the code by one character every two attempts. Sustained
        # collisions are evidence the keyspace is filling up; widening it stays
        # ahead of that rather than failing harder.
        candidate_length = base_length + (attempt // 2)
        candidate = generate_candidate(candidate_length)

        if is_reserved(candidate):
            continue

        if try_claim(candidate):
            return candidate

    raise ShortCodeCollisionError(
        f"Could not generate a unique short code after {retries + 1} attempts."
    )


class ShortCodeCollisionError(Exception):
    """Raised when a unique short code could not be generated."""
