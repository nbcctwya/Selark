"""Subscription resources and bounded, explicit reads of provider-reported quota.

Provider quota units never become tokens, cash, or locally inferred reset schedules.
"""

import asyncio
import math
from datetime import datetime
from typing import Literal

import httpx
from pydantic import Field

from selark.config import StrictConfig
from selark.errors import GatewayError
from selark.protocol import strict_json

PLAN_PRESETS = {
    "kimi-code": {
        "name": "Kimi Coding Plan",
        "base_urls": ["https://api.kimi.com/coding/v1", "https://api.kimi.ai/coding/v1"],
        "models": [
            "kimi-for-coding",
            "kimi-for-coding-highspeed",
            "k3",
            "k3-256k",
            "k3.1-pro",
            "k3.1-pro-highspeed",
        ],
        "default_model": "kimi-for-coding",
        "quota_path": "/usages",
        "generation_support": "personal_interactive",
        "docs": "https://www.kimi.com/code/docs/",
    },
    "glm-coding": {
        "name": "GLM Coding Plan (preset only)",
        "base_urls": ["https://open.bigmodel.cn/api/coding/paas/v4"],
        "models": ["glm-5.3", "glm-5.3-flash"],
        "default_model": "glm-5.3-flash",
        "quota_path": None,
        "generation_support": "preset_only",
        "docs": "https://docs.bigmodel.cn/cn/coding-plan/quick-start",
    },
}


class PlanSettings(StrictConfig):
    # Tier is an owner-supplied label, not proof of membership or an inferred allowance.
    tier: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$"
    )
    use: Literal["personal_interactive_coding", "personal_interactive"] = (
        "personal_interactive_coding"
    )
    reasoning_effort: Literal["low", "high", "max"] = "low"
    extra_usage_policy: Literal["forbid"] = "forbid"


def interaction_purpose(config, interaction):
    """Require an explicit purpose; old connections retain their narrower scope."""
    if interaction == "personal_coding":
        return "interactive_coding"
    if interaction == "personal_interactive" and config.plan.use == "personal_interactive":
        return "interactive_task"
    raise GatewayError(
        403,
        "plan_interaction_required",
        "套餐需明确声明个人交互用途；通用个人任务还需本机管理员启用 "
        "plans set --use personal_interactive。既有编程用途可继续使用 personal_coding。",
    )


def empty_quota(state="never"):
    return {
        "state": state,
        "checked_at": None,
        "windows": [],
        "reported_counters": [],
        "remaining_tokens": None,
        "extra_usage": "unknown",
        "membership_status": "unknown",
        "source": None,
        "conflicting_fields": False,
        "invalid_fields": False,
    }


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value if parsed.tzinfo is not None else None
    except ValueError:
        return None


def counter(value):
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        return value if int(value) <= 2**63 - 1 else None
    return None


def parse_quota(data):
    if not isinstance(data, dict):
        raise ValueError("invalid quota")
    result = empty_quota("partial")
    usages = data.get("usages")
    if isinstance(usages, dict):
        for name in ("limit_5h", "limit_7d", "limit_month_total", "limit_month_code"):
            raw = usages.get(name)
            if not isinstance(raw, dict):
                continue
            ratio = raw.get("used_ratio")
            if isinstance(ratio, str):
                try:
                    ratio = float(ratio)
                except ValueError:
                    ratio = None
            if type(ratio) not in {int, float} or not 0 <= ratio <= 1 or not math.isfinite(ratio):
                ratio = None
            if raw.get("used_ratio") is not None and ratio is None:
                result["invalid_fields"] = True
            reset = timestamp(raw.get("reset_time"))
            if raw.get("reset_time") is not None and reset is None:
                result["invalid_fields"] = True
            if ratio is not None or reset is not None:
                result["windows"].append({"name": name, "used_ratio": ratio, "reset_at": reset})
    counters = []
    if isinstance(data.get("usage"), dict):
        counters.append(("unscoped", data["usage"]))
    limits = data.get("limits")
    if isinstance(limits, list) and len(limits) <= 32:
        for entry in limits:
            if not isinstance(entry, dict) or not isinstance(entry.get("detail"), dict):
                continue
            window = entry.get("window")
            # Only label the duration explicitly reported by the provider.
            if (
                isinstance(window, dict)
                and window.get("timeUnit") == "TIME_UNIT_MINUTE"
                and counter(window.get("duration"))
            ):
                counters.append(("minutes:" + counter(window["duration"]), entry["detail"]))
    for window, detail in counters:
        if any(
            detail.get(key) is not None and counter(detail[key]) is None
            for key in ("limit", "used", "remaining")
        ):
            result["invalid_fields"] = True
        if detail.get("resetTime") is not None and timestamp(detail["resetTime"]) is None:
            result["invalid_fields"] = True
        result["reported_counters"].append(
            {
                "window": window,
                "unit": "provider_units",
                **{key: counter(detail.get(key)) for key in ("limit", "used", "remaining")},
                "reset_at": timestamp(detail.get("resetTime")),
            }
        )
    # Different serializers can contradict one another. Do not choose a favourable value.
    for record in result["reported_counters"]:
        limit, used, remaining = (record[key] for key in ("limit", "used", "remaining"))
        if limit is not None and remaining is not None and int(remaining) > int(limit):
            result["conflicting_fields"] = True
        if limit is not None and used is not None:
            limit, used = int(limit), int(used)
            if used > limit or (remaining is not None and int(remaining) + used != limit):
                result["conflicting_fields"] = True
            for window in result["windows"]:
                match = record["window"] == "minutes:300" and window["name"] == "limit_5h"
                if record["reset_at"] and window["reset_at"]:
                    left = datetime.fromisoformat(record["reset_at"].replace("Z", "+00:00"))
                    right = datetime.fromisoformat(window["reset_at"].replace("Z", "+00:00"))
                    match = match or abs((left - right).total_seconds()) <= 2
                if (
                    limit > 0
                    and match
                    and window["used_ratio"] is not None
                    and abs(used / limit - window["used_ratio"]) > 0.011
                ):
                    result["conflicting_fields"] = True
    wallets = [data[key] for key in ("booster_wallet", "boosterWallet") if key in data]
    statuses = [wallet.get("status") if isinstance(wallet, dict) else None for wallet in wallets]
    if statuses and all(value == "STATUS_DISABLED" for value in statuses):
        result["extra_usage"] = "disabled"
    elif "STATUS_ENABLED" in statuses or "STATUS_ACTIVE" in statuses:
        result["extra_usage"] = "enabled"
    # Null/omitted wallet is not proof that automatic paid overage is disabled.
    if any(w["used_ratio"] is not None for w in result["windows"]):
        result["state"] = "reported"
    if result["conflicting_fields"]:
        result["state"] = "inconsistent"
    if result["invalid_fields"]:
        result["state"] = "invalid"
    if not result["windows"] and not result["reported_counters"]:
        raise ValueError("quota fields absent")
    return result


async def query_quota(config, *, client=None, transport=None):
    from selark.providers.factory import provider_for
    from selark.providers.mimo import close_response

    preset = PLAN_PRESETS[config.provider]
    if preset["quota_path"] is None:
        return empty_quota("unsupported") | {"reason": "未确认支持 API Key 的官方额度查询接口。"}
    if client is None:
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as owned:
            return await query_quota(config, client=owned)
    try:
        async with asyncio.timeout(10):
            response = await provider_for(config, client).request("GET", preset["quota_path"])
            try:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError
                result = parse_quota(strict_json(body))
            finally:
                await close_response(response)
        result["source"] = config.base_url + preset["quota_path"]
        return result
    except GatewayError as error:
        raise GatewayError(
            error.status,
            error.code,
            "套餐额度查询失败；未生成。" + error.message,
            error.upstream_status,
            retry_after=error.retry_after,
        ) from None
    except (TimeoutError, httpx.HTTPError):
        raise GatewayError(
            503, "plan_quota_unavailable", "套餐额度查询超时或不可达；未生成。"
        ) from None
    except (ValueError, UnicodeError):
        raise GatewayError(
            503, "plan_quota_invalid", "套餐额度响应缺少可验证字段；未生成。"
        ) from None


def enforce_quota(snapshot):
    if snapshot["extra_usage"] != "disabled":
        raise GatewayError(
            403,
            "plan_extra_usage_unverified",
            "未确认供应商加油包已关闭；请在官方控制台关闭后刷新，Selark 不自动启用额外付费。",
        )
    if snapshot.get("invalid_fields"):
        raise GatewayError(503, "plan_quota_invalid", "供应商额度字段无效；未生成。")
    if snapshot["conflicting_fields"]:
        raise GatewayError(
            503, "plan_quota_inconsistent", "供应商额度字段矛盾，请在官方控制台核对；未生成。"
        )
    if not any(w["used_ratio"] is not None for w in snapshot["windows"]) and not any(
        r["remaining"] is not None for r in snapshot["reported_counters"]
    ):
        raise GatewayError(503, "plan_quota_unknown", "未取得有效的剩余额度状态；未生成。")
    if any(
        window["used_ratio"] is not None and window["used_ratio"] >= 1
        for window in snapshot["windows"]
    ):
        raise GatewayError(
            429, "plan_quota_exhausted", "供应商报告套餐窗口已用尽；等待官方刷新，不切换付费来源。"
        )
    if any(
        r["remaining"] is not None and int(r["remaining"]) == 0
        for r in snapshot["reported_counters"]
    ):
        raise GatewayError(
            429, "plan_quota_exhausted", "供应商报告套餐额度已用尽；不重试或切换来源。"
        )
