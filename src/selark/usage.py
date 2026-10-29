"""Validate reported counts without manufacturing usage or provider invoices."""

MAX_COUNT = 2**63 - 1


def valid_count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def inconsistent_usage(usage):
    if not isinstance(usage, dict):
        return False
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
    for key, value in usage.items():
        if valid_count(value):
            if key in peak and value < peak[key]:
                state["usage_regressed"] = True
            peak[key] = max(value, peak.get(key, 0))
    state["usage"] = usage
