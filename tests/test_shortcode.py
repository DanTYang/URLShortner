"""Unit tests for short-code generation and validation (no AWS needed)."""

import pytest

from urlshortener_common import config, shortcode


def test_generate_candidate_length_and_alphabet():
    code = shortcode.generate_candidate(7)
    assert len(code) == 7
    assert all(c in config.BASE62_ALPHABET for c in code)


def test_generate_candidate_respects_configured_length():
    code = shortcode.generate_candidate()
    assert len(code) == config.SHORT_CODE_LENGTH


def test_valid_custom_codes():
    assert shortcode.is_valid_custom_code("spring-sale")
    assert shortcode.is_valid_custom_code("my_link_2026")
    assert shortcode.is_valid_custom_code("abc")


@pytest.mark.parametrize(
    "bad",
    [
        "ab",                 # too short
        "has spaces",
        "emoji😀",
        "with/slash",
        "urls",               # reserved
        "API",                # reserved (case-insensitive)
        "",
    ],
)
def test_invalid_custom_codes(bad):
    assert not shortcode.is_valid_custom_code(bad)


def test_generate_unique_code_returns_first_success():
    """When the first candidate is free, it is used immediately."""
    calls = []

    def try_claim(code):
        calls.append(code)
        return True  # always free

    result = shortcode.generate_unique_code(try_claim)
    assert result == calls[0]
    assert len(calls) == 1


def test_generate_unique_code_retries_on_collision():
    """Collisions trigger retries with fresh candidates until one sticks."""
    seen = []

    def try_claim(code):
        seen.append(code)
        return len(seen) >= 3  # first two "collide"

    result = shortcode.generate_unique_code(try_claim, max_retries=5)
    assert result == seen[-1]
    assert len(seen) == 3
    # Every candidate should be distinct.
    assert len(set(seen)) == len(seen)


def test_generate_unique_code_gives_up_after_max_retries():
    def always_collide(code):
        return False

    with pytest.raises(shortcode.ShortCodeCollisionError):
        shortcode.generate_unique_code(always_collide, max_retries=3)


def test_code_grows_after_repeated_collisions():
    """Sustained collisions widen the keyspace by lengthening the code."""
    lengths = []

    def try_claim(code):
        lengths.append(len(code))
        return len(lengths) >= 4

    base = config.SHORT_CODE_LENGTH
    shortcode.generate_unique_code(try_claim, max_retries=6)
    # By the 3rd/4th attempt the length should have grown beyond the base.
    assert max(lengths) > base
