"""Validate reported counts without manufacturing usage or provider invoices."""

MAX_COUNT = 2**63 - 1
COUNTERS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
DETAILS = {
    "prompt_tokens_details": (
        "cached_tokens",
        "cache_write_tokens",
        "audio_tokens",
        "image_tokens",
        "video_tokens",
    ),
    "completion_tokens_details": ("reasoning_tokens", "audio_tokens"),
    "cache_creation": ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
    "output_tokens_details": ("thinking_tokens",),
}


def sanitize_usage(usage):
    if not isinstance(usage, dict):
        return None
    result, invalid = {}, set()
    known_paths = (
        set(COUNTERS)
        | set(DETAILS)
        | {key + "." + field for key, fields in DETAILS.items() for field in fields}
    )
    if isinstance(usage.get("_invalid_fields"), list):
        invalid.update(
            path
            for path in usage["_invalid_fields"]
            if isinstance(path, str) and path in known_paths
        )

    def copy_counts(source, destination, fields, prefix=""):
        for key in fields:
            if key in source:
                value = source[key]
                if value is None or type(value) in {int, float, bool}:
                    destination[key] = value
                else:
                    invalid.add(prefix + key)

    copy_counts(usage, result, COUNTERS)
    for key, fields in DETAILS.items():
        if key not in usage:
            continue
        if usage[key] is None:
            result[key] = None
        elif isinstance(usage[key], dict):
            result[key] = {}
            copy_counts(usage[key], result[key], fields, key + ".")
        else:
            invalid.add(key)
    if invalid:
        result["_invalid_fields"] = sorted(invalid)
    return result or None


def valid_count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def inconsistent_usage(usage):
    if not isinstance(usage, dict):
        return False
    if usage.get("_invalid_fields"):
        return True
    for key in COUNTERS:
        if usage.get(key) is not None and not valid_count(usage[key]):
            return True
    for key, fields in DETAILS.items():
        if usage.get(key) is None:
            continue
        if not isinstance(usage[key], dict) or any(
            usage[key].get(field) is not None and not valid_count(usage[key][field])
            for field in fields
        ):
            return True
    prompt, completion, total = (
        usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if valid_count(total):
        if any(type(v) is int and not valid_count(v) for v in (prompt, completion)):
            return True
        parts = [v for v in (prompt, completion) if valid_count(v)]
        if sum(parts) > total or (len(parts) == 2 and sum(parts) != total):
            return True
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens"))
    if (
        valid_count(usage.get("prompt_cache_hit_tokens"))
        and valid_count(details.get("cached_tokens"))
        and usage["prompt_cache_hit_tokens"] != details["cached_tokens"]
    ):
        return True
    if valid_count(prompt):
        if valid_count(cached) and cached > prompt:
            return True
        writes = details.get("cache_write_tokens")
        if valid_count(writes) and writes + (cached if valid_count(cached) else 0) > prompt:
            return True
        missed = usage.get("prompt_cache_miss_tokens")
        if valid_count(cached) and valid_count(missed) and cached + missed != prompt:
            return True
    return False


def observed_token_floor(usage, provider):
    """Keep uncertain reservations at least as large as the observed counters."""
    if not isinstance(usage, dict):
        return 0
    fields = ("prompt_tokens", "completion_tokens")
    if provider == "anthropic":
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    parts = sum(usage[k] for k in fields if valid_count(usage.get(k)))
    total = usage.get("total_tokens")
    return min(MAX_COUNT, max(parts, total if valid_count(total) else 0))


def observe_usage(state, usage):
    """Cumulative stream counters may repeat, but must not move backwards."""
    peak = state.setdefault("usage_peak", {})

    def observe(previous, current):
        for key, value in current.items():
            if isinstance(value, dict):
                observe(previous.setdefault(key, {}), value)
            elif valid_count(value):
                if key in previous and value < previous[key]:
                    state["usage_regressed"] = True
                previous[key] = max(value, previous.get(key, 0))

    observe(peak, usage)
    state["usage"] = usage
