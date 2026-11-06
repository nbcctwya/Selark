"""Shared Coding Plan fixtures with fake keys, quotas and upstream responses."""

import httpx
import pytest

from .protocol import completion

MODEL = "kimi-for-coding"


PLAN = "personal-code"


PAYLOAD = {
    "connection_id": PLAN,
    "messages": [{"role": "user", "content": "Fix this Python: return 1 + 1"}],
    "max_tokens": 32,
    "interaction": "personal_coding",
}


def quota():
    return {
        "usages": {
            "limit_5h": {"used_ratio": 0, "reset_time": "2026-10-28T15:00:00Z"},
            "limit_7d": {"used_ratio": 0.413, "reset_time": "2026-10-30T00:00:00Z"},
        },
        "booster_wallet": {"status": "STATUS_DISABLED", "private_user_id": "must-not-save"},
        "usage": {
            "limit": "100",
            "used": "41",
            "remaining": "59",
            "resetTime": "2026-10-30T00:00:00.100Z",
        },
    }


def reply(usage=True):
    data = completion(usage) | {"model": MODEL}
    data["choices"][0]["message"]["reasoning_content"] = "Check the addition."
    return data


@pytest.fixture
def plan(runtime):
    runtime.add_connection(PLAN, "kimi-code", "fake-plan-key", allowed_models=[MODEL])
    return runtime.connection_settings()[-1]


def caller(runtime, **patch):
    return runtime.store.create_token(
        "personal", policy={"allow_token_plans": True, "allowed_connections": [PLAN], **patch}
    )


def handler(calls, *, quota_data=None, response=None):
    def upstream(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fake-plan-key"
        assert request.headers["user-agent"].startswith("Selark/")
        assert "Claude" not in request.headers["user-agent"]
        if request.method == "GET":
            assert request.url.path == "/coding/v1/usages"
            return httpx.Response(200, json=quota_data if quota_data is not None else quota())
        assert request.url.path == "/coding/v1/chat/completions"
        return response if response is not None else httpx.Response(200, json=reply())

    return upstream
