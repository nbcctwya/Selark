"""Bounded-depth, finite-number JSON for data received from upstreams."""

import json
import math


def strict_json(data):
    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")

    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value

    try:
        result = json.loads(data, parse_constant=invalid_constant, parse_float=finite_float)
    except RecursionError:
        raise ValueError("JSON nesting exceeds the supported depth") from None
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting exceeds the supported depth")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    return result
