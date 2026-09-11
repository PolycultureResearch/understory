"""Turn warehouse driver values into JSON-safe Python values.

Both adapters return `Result.rows` that must survive `json.dumps` unchanged,
because the MCP layer serialises tool responses without a custom encoder.
"""

from __future__ import annotations

import base64
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID


def json_safe(value: Any) -> Any:
    """Convert one cell value. Dates and datetimes become ISO strings, Decimal
    becomes float, NaN and infinities become None, bytes become base64, and
    containers are converted recursively."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return None
        return float(value)
    if isinstance(value, datetime):
        # datetime is a subclass of date, so check it first.
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    return str(value)


def json_safe_row(row: Any) -> list[Any]:
    return [json_safe(v) for v in row]


def as_date(value: Any) -> date | None:
    """Coerce a MAX(column) result to a date. Accepts date, datetime, or an
    ISO string. Returns None for None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    raise TypeError(f"cannot interpret {value!r} as a date")
