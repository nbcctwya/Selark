"""Local application policies, explicit aliases and decimal price estimates.

These estimates are admission controls, never provider invoices or token measurements.
"""

import json
import re
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from selark.config import ConfigError, StrictConfig
from selark.connections import valid_model
from selark.errors import GatewayError
from selark.usage import MAX_COUNT, inconsistent_usage, observed_token_floor, valid_count


def decimal_text(value):
    return format(value, "f")


def money_sum(values):
    with localcontext() as context:
        context.prec = 60
        return decimal_text(sum((Decimal(value) for value in values), Decimal(0)))


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,10}(?:\.\d{1,9})?", value):
        raise ValueError("金额必须是非负十进制字符串，最多九位小数")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("金额无效") from None
    if not result.is_finite() or result > Decimal("1000000000"):
        raise ValueError("金额超出范围")
    return decimal_text(result)


class Policy(StrictConfig):
    allow_token_plans: bool = False
    allow_chatgpt_subscriptions: bool = False
    chatgpt_output_reservation: int | None = Field(default=None, ge=1, le=131072)
    allowed_connections: list[str] | None = Field(default=None, max_length=32)
    allowed_models: list[str] | None = Field(default=None, max_length=1000)
    requests_per_minute: int | None = Field(default=None, ge=1, le=100000)
    max_concurrent_requests: int | None = Field(default=None, ge=1, le=256)
    max_output_tokens: int | None = Field(default=None, ge=1, le=131072)
    max_estimated_input_tokens: int | None = Field(default=None, ge=1, le=2000000)
    daily_token_limit: int | None = Field(default=None, ge=1, le=10**12)
    daily_budget: str | None = None
    budget_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")

    @field_validator("daily_budget")
    @classmethod
    def valid_budget(cls, value):
        return money(value) if value is not None else None

    @model_validator(mode="after")
    def valid_access(self):
        for connection in self.allowed_connections or []:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", connection):
                raise ValueError("连接 ID 无效")
        for qualified in self.allowed_models or []:
            parts = qualified.split("::")
            if (
                len(parts) != 2
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", parts[0])
                or not valid_model(parts[1])
            ):
                raise ValueError("允许模型必须使用 connection::model")
        return self


class Target(StrictConfig):
    connection_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    model: str

    @field_validator("model")
    @classmethod
    def model_id(cls, value):
        if not valid_model(value):
            raise ValueError("模型标识无效")
        return value


class Price(Target):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_per_million: str
    output_per_million: str
    cached_input_per_million: str | None = None
    cache_write_per_million: str | None = None
    note: str = Field(default="", max_length=300)

    @field_validator(
        "input_per_million",
        "output_per_million",
        "cached_input_per_million",
        "cache_write_per_million",
    )
    @classmethod
    def valid_price(cls, value):
        return money(value) if value is not None else None


class ResourcesConfig(StrictConfig):
    schema_version: Literal[1] = 1
    aliases: dict[str, Target] = Field(default_factory=dict, max_length=128)
    prices: list[Price] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def unique(self):
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key) for key in self.aliases):
            raise ValueError("别名无效")
        identities = [(p.connection_id, p.model) for p in self.prices]
        if len(identities) != len(set(identities)):
            raise ValueError("价格配置重复")
        return self


def permitted(policy, connection, model, resource_type="api"):
    plan_allowed = resource_type != "token_plan" or (
        policy.allow_token_plans
        and policy.allowed_connections is not None
        and connection in policy.allowed_connections
    )
    return (
        plan_allowed
        and (
            resource_type != "chatgpt_subscription"
            or (
                policy.allow_chatgpt_subscriptions
                and policy.allowed_connections is not None
                and connection in policy.allowed_connections
            )
        )
        and (policy.allowed_connections is None or connection in policy.allowed_connections)
        and (policy.allowed_models is None or f"{connection}::{model}" in policy.allowed_models)
    )


def enforce_access(policy, connection, model, resource_type="api"):
    if not permitted(policy, connection, model, resource_type):
        raise GatewayError(403, "model_access_denied", "此调用方没有使用该连接或模型的权限。")


def input_estimate(payload):
    # Deliberately conservative heuristic; not a model tokenizer or a guaranteed upper bound.
    text = json.dumps(
        {
            key: payload[key]
            for key in ("messages", "system", "input", "instructions")
            if key in payload
        },
        ensure_ascii=False,
    )
    return (
        len(text.encode("utf-8"))
        + 256
        + 32 * len(payload.get("messages", payload.get("input", [])))
    )


def estimate_cost(price, input_count, output_count, cached=0, writes=0):
    regular = Decimal(price["input_per_million"])
    read = Decimal(price.get("cached_input_per_million") or price["input_per_million"])
    write = Decimal(price.get("cache_write_per_million") or price["input_per_million"])
    with localcontext() as context:
        context.prec = 60
        amount = (
            input_count * regular
            + output_count * Decimal(price["output_per_million"])
            + cached * read
            + writes * write
        ) / Decimal(1000000)
        return decimal_text(amount.quantize(Decimal("0.000000001"), rounding=ROUND_UP))


def measured_tokens(usage, provider):
    if not isinstance(usage, dict) or inconsistent_usage(usage):
        return None, "unknown"
    if valid_count(usage.get("total_tokens")):
        return usage["total_tokens"], "reported_total"
    if provider == "anthropic":
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        if all(valid_count(usage.get(f)) for f in fields):
            value = sum(usage[f] for f in fields)
            if value <= MAX_COUNT:
                return value, "derived_components"  # Never written into reported total_tokens.
    return None, "unknown"


def usage_cost(quote, usage, provider):
    if not quote or not isinstance(usage, dict) or inconsistent_usage(usage):
        return None
    if provider == "anthropic":
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        if not all(valid_count(usage.get(f)) for f in fields):
            return None
        return estimate_cost(quote, *(usage[f] for f in fields))
    if not all(valid_count(usage.get(f)) for f in ("prompt_tokens", "completion_tokens")):
        return None
    cached, writes = 0, 0
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    if quote.get("cached_input_per_million") is not None:
        cached = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens"))
        if not valid_count(cached) or cached > usage["prompt_tokens"]:
            return None
    if quote.get("cache_write_per_million") is not None:
        writes = details.get("cache_write_tokens")
        if not valid_count(writes) or writes + cached > usage["prompt_tokens"]:
            return None
    return estimate_cost(
        quote, usage["prompt_tokens"] - cached - writes, usage["completion_tokens"], cached, writes
    )


def policy_for(db, caller):
    if caller == "local-probe":
        return Policy()
    row = db.execute("SELECT policy_json FROM tokens WHERE id=?", (caller,)).fetchone()
    if row is None:
        raise GatewayError(403, "unknown_caller", "调用方身份不存在。")
    try:
        return Policy.model_validate_json(row[0])
    except ValidationError:
        raise ConfigError("调用方策略损坏，请检查记录库和备份。") from None


def allocations(db, caller, day):
    return db.execute(
        "SELECT * FROM allocations WHERE caller_id=? AND day=?", (caller, day)
    ).fetchall()


def reserve(
    db,
    request_id,
    caller,
    connection,
    model,
    provider,
    estimate,
    output,
    price,
    now,
    alias,
    resource_type="api",
    plan=None,
):
    policy = policy_for(db, caller)
    enforce_access(policy, connection, model, resource_type)
    if resource_type in {"token_plan", "chatgpt_subscription"}:
        if policy.daily_budget is not None:
            raise GatewayError(
                403,
                "plan_budget_unsupported",
                "订阅消耗不能折算为按量费用预算；请使用 Token、并发和频率限制。",
            )
        price = None
    if resource_type == "chatgpt_subscription":
        if policy.max_output_tokens is not None:
            raise GatewayError(
                403,
                "chatgpt_output_cap_unsupported",
                "官方订阅接口不支持硬输出上限；此调用方策略不兼容。",
            )
        if policy.chatgpt_output_reservation is None or output != policy.chatgpt_output_reservation:
            raise GatewayError(
                403,
                "chatgpt_reservation_required",
                "请明确配置 chatgpt_output_reservation；这是本地估算预留，不是上游输出硬限制。",
            )
    if policy.max_output_tokens is not None and output > policy.max_output_tokens:
        raise GatewayError(403, "output_limit_exceeded", "请求输出上限超过此调用方允许值。")
    if (
        policy.max_estimated_input_tokens is not None
        and estimate > policy.max_estimated_input_tokens
    ):
        raise GatewayError(403, "input_estimate_exceeded", "输入估算超过此调用方允许值。")
    if policy.requests_per_minute:
        since = (datetime.fromisoformat(now) - timedelta(seconds=60)).isoformat(
            timespec="microseconds"
        )
        count = db.execute(
            "SELECT COUNT(*) FROM allocations WHERE caller_id=? AND created_at>?", (caller, since)
        ).fetchone()[0]
        if count >= policy.requests_per_minute:
            raise GatewayError(429, "caller_rate_limit", "此调用方最近 60 秒的请求次数已达上限。")
    day = now[:10]
    rows = (
        allocations(db, caller, day)
        if policy.daily_token_limit is not None or policy.daily_budget is not None
        else []
    )
    total = estimate + output
    quote = price.model_dump(mode="json") if price else None
    reserve_cost = None
    if quote:
        upper = dict(quote)
        upper["input_per_million"] = max(
            (
                quote[k]
                for k in (
                    "input_per_million",
                    "cached_input_per_million",
                    "cache_write_per_million",
                )
                if quote.get(k) is not None
            ),
            key=Decimal,
        )
        reserve_cost = estimate_cost(upper, estimate, output)
    if policy.daily_token_limit is not None:
        if any(row["token_basis"] == "unknown_hold" for row in rows):
            raise GatewayError(
                429,
                "quota_usage_unknown",
                "今日有用量未知的已发送请求，保留预留并暂停此调用方的新生成。",
            )
        if sum(row["quota_tokens"] for row in rows) + total > policy.daily_token_limit:
            raise GatewayError(
                429, "caller_token_quota", "此调用方今日 Token 配额不足，包含在途预留。"
            )
    if policy.daily_budget is not None:
        if not quote or quote["currency"] != policy.budget_currency:
            raise GatewayError(
                403, "budget_price_unavailable", "预算限制要求配置同币种的模型价格。"
            )
        if any(
            row["quota_cost"] is None
            or row["currency"] != policy.budget_currency
            or row["cost_status"] == "unknown"
            for row in rows
        ):
            raise GatewayError(
                429,
                "budget_cost_unknown",
                "今日存在费用未知或其他币种的调用，暂停此调用方的新生成。",
            )
        if Decimal(money_sum([*(row["quota_cost"] for row in rows), reserve_cost])) > Decimal(
            policy.daily_budget
        ):
            raise GatewayError(
                429, "caller_budget_quota", "此调用方今日估算预算不足，包含在途预留。"
            )
    document = {
        "resource_type": resource_type,
        "plan_id": connection if resource_type in {"token_plan", "chatgpt_subscription"} else None,
        "plan_snapshot": plan,
        "provider": provider,
        "requested_model": model,
        "requested_alias": alias,
        "estimated_input_tokens": estimate,
        "output_limit": None if resource_type == "chatgpt_subscription" else output,
        "reserved_tokens": total,
        "reserved_cost": reserve_cost,
        "price_snapshot": quote,
        "estimated_cost": None,
        "billed_cost": None,
    }
    if resource_type == "chatgpt_subscription":
        document["estimated_output_reservation"] = output
    db.execute(
        "INSERT INTO allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            request_id,
            caller,
            day,
            now,
            "reserved",
            total,
            "estimated_reservation",
            reserve_cost,
            quote["currency"] if quote else None,
            "reserved" if quote else "not_priced",
            None,
            json.dumps(document),
        ),
    )


def settle(db, request_id, state):
    row = db.execute("SELECT * FROM allocations WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        return
    if state.get("upstream_attempts") == 0 and state.get("upstream_result") == "not_started":
        # A cancelled await can outlive the worker committing its reservation.
        # Only an explicit no-dispatch receipt proves the reservation is unused;
        # crash recovery without this evidence must retain its unknown hold.
        db.execute("DELETE FROM allocations WHERE request_id=?", (request_id,))
        return
    try:
        document = json.loads(row["document"])
        if not isinstance(document, dict) or not all(
            key in document
            for key in (
                "provider",
                "requested_model",
                "price_snapshot",
                "reserved_tokens",
                "reserved_cost",
            )
        ):
            raise ValueError
    except ValueError:
        raise ConfigError("额度预留记录损坏，请检查记录库和备份；未重发上游请求。") from None
    complete = bool(state.get("usage_final")) and state.get("upstream_result") in {
        "success",
        "failed",
    }
    trustworthy = complete and not state.get("usage_regressed")
    tokens, basis = (
        measured_tokens(state.get("usage"), document["provider"])
        if trustworthy
        else (None, "unknown")
    )
    quote = document["price_snapshot"]
    amount = usage_cost(quote, state.get("usage"), document["provider"]) if trustworthy else None
    if state.get("actual_model") != document["requested_model"]:
        amount = None  # A different returned model has no safely matched price snapshot.
    token_basis = basis if tokens is not None else "unknown_hold"
    cost_status = "estimated" if amount is not None else "unknown" if quote else "not_priced"
    document.update(
        estimated_cost=amount,
        observed_usage_peak=state.get("usage_peak"),
        usage_consistency="regressed"
        if state.get("usage_regressed")
        else "inconsistent"
        if inconsistent_usage(state.get("usage"))
        else "uncontradicted",
    )
    db.execute(
        "UPDATE allocations SET state=?,quota_tokens=?,token_basis=?,quota_cost=?,cost_status=?,"
        "estimated_cost=?,document=? WHERE request_id=?",
        (
            "settled" if complete else "uncertain",
            tokens
            if tokens is not None
            else max(
                document["reserved_tokens"],
                observed_token_floor(state.get("usage"), document["provider"]),
                observed_token_floor(state.get("usage_peak"), document["provider"]),
            ),
            token_basis,
            amount if amount is not None else document["reserved_cost"],
            cost_status,
            amount,
            json.dumps(document),
            request_id,
        ),
    )


def public_allocation(row):
    if row is None:
        return None
    value = json.loads(row["document"])
    value.update(
        {
            key: row[key]
            for key in (
                "state",
                "quota_tokens",
                "token_basis",
                "quota_cost",
                "currency",
                "cost_status",
            )
        }
    )
    return value
