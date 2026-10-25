"""Common validation helpers for model adapter configuration objects."""
from __future__ import annotations

import math
from urllib.parse import urlsplit


def validate_http_url(value, message: str) -> None:
    """Require a credential-free HTTP(S) URL without query or fragment."""
    try:
        parsed = urlsplit(value)
        valid = (
            isinstance(value, str)
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() for char in value)
        )
        parsed.port
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(message)


def validate_finite_positive(value, message: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(message)


def validate_positive_int(value, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(message)
