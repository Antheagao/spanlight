"""Make a decoded AnyValue safe to store as JSON.

SQLite's JSON and the browser's `JSON.parse` both choke on what Python's
`json` writes by default: `NaN` and `Infinity` are not JSON, and integers
beyond 2**53 lose precision in a JavaScript number.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

from .model import AnyValue

# The largest integer a JavaScript number holds exactly.
MAX_SAFE_INTEGER = 2**53 - 1


def json_safe(value: AnyValue) -> Any:
    """Non-finite doubles and integers beyond +-(2**53 - 1) become strings,
    bytes become base64 text, and containers are converted recursively."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return {key: json_safe(item) for key, item in value.items()}


def dumps_json_safe(value: AnyValue) -> str:
    """The stored form. `allow_nan=False` makes a stray NaN an error here
    instead of invalid JSON in the database."""
    return json.dumps(json_safe(value), allow_nan=False, ensure_ascii=False)
