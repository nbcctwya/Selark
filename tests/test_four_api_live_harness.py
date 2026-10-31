"""The paid concurrency harness must bound egress before any provider socket is used."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from selark.connections import ConnectionConfig

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("four_api_live", SCRIPTS / "verify_four_api_live.py")
harness = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(harness)
finally:
    sys.path.remove(str(SCRIPTS))


def connection():
    return ConnectionConfig(
        connection_id="openai-live",
        name="Fake acceptance target",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="fake-live-key",
        allowed_models=["gpt-6-luna"],
        default_model="gpt-6-luna",
    )


def payload():
    return {
        "model": "gpt-6-luna",
        "messages": [{"role": "user", "content": "test"}],
        "max_completion_tokens": 16,
        "stream": False,
    }


def test_concurrent_attempts_are_reserved_before_send_and_budget_cannot_be_reset(tmp_path):
    guard = harness.LiveGuard(tmp_path, [connection()])
    sent = []

    def upstream(request):
        saved = json.loads((tmp_path / "budget.json").read_text())
        assert saved["attempted_requests"] > len(sent)
        sent.append(request)
        raise httpx.ReadTimeout("Invented response timeout", request=request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(upstream),
            event_hooks={"request": [guard.before]},
            headers={"Authorization": "Bearer fake-live-key"},
        ) as client:
            results = await asyncio.gather(
                *(
                    client.post("https://api.openai.com/v1/chat/completions", json=payload())
                    for _ in range(30)
                ),
                return_exceptions=True,
            )
        assert sum(isinstance(r, httpx.ReadTimeout) for r in results) == 7
        assert sum(isinstance(r, httpx.ConnectError) for r in results) == 23

    asyncio.run(run())
    assert len(sent) == 7
    assert json.loads((tmp_path / "budget.json").read_text())["attempted_requests"] == 7
    with pytest.raises(FileExistsError):
        harness.LiveGuard(tmp_path, [connection()])


@pytest.mark.parametrize("case", ["destination", "model", "output", "input", "credential", "stop"])
def test_unapproved_or_unbounded_requests_never_reach_transport(tmp_path, case):
    guard = harness.LiveGuard(tmp_path, [connection()])
    body, url, key = payload(), "https://api.openai.com/v1/chat/completions", "fake-live-key"
    if case == "destination":
        url = "https://unexpected.invalid/v1/chat/completions"
    elif case == "model":
        body["model"] = "unapproved-model"
    elif case == "output":
        body["max_completion_tokens"] = 257
    elif case == "input":
        body["messages"][0]["content"] = "x" * 1500
    elif case == "credential":
        key = "wrong-provider-key"
    elif case == "stop":
        (tmp_path / "STOP").touch()

    def forbidden(request):
        pytest.fail("Rejected requests must not reach the upstream transport")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden),
            event_hooks={"request": [guard.before]},
            headers={"Authorization": "Bearer " + key},
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(url, json=body)

    asyncio.run(run())
    assert not guard.events and not guard.counts


def test_provider_ignoring_output_cap_stops_all_future_requests_without_retry(tmp_path):
    guard = harness.LiveGuard(tmp_path, [connection()])
    result = {
        "mode": "limit",
        "output_cap": 8,
        "http_status": 200,
        "done": True,
        "finish_reason": "stop",
        "usage": {"completion_tokens": 974},
    }
    harness.assess_result(result)
    guard.observe_result(result)
    assert not result["acceptance_passed"] and result["output_cap_respected"] is False
    assert (tmp_path / "STOP").exists()

    def forbidden(request):
        pytest.fail("An output-limit violation must stop later paid requests")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden),
            event_hooks={"request": [guard.before]},
            headers={"Authorization": "Bearer fake-live-key"},
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post("https://api.openai.com/v1/chat/completions", json=payload())

    asyncio.run(run())
    assert not guard.events
    assert json.loads((tmp_path / "budget.json").read_text())["stopped_reason"]


def test_reasoning_only_truncation_and_cancelled_unknown_usage_are_not_answer_success():
    limited = {
        "mode": "limit",
        "output_cap": 8,
        "http_status": 200,
        "done": True,
        "finish_reason": "length",
        "correct_response": False,
        "usage": {"completion_tokens": 8},
    }
    harness.assess_result(limited)
    assert limited["acceptance_passed"]
    cancelled = {
        "mode": "cancel",
        "output_cap": 64,
        "http_status": 200,
        "done": False,
        "closed_after_first_text": True,
        "usage": None,
    }
    harness.assess_result(cancelled)
    assert cancelled["acceptance_passed"] and cancelled["output_cap_respected"] is None
    cancelled["usage"] = {"completion_tokens": 65}
    harness.assess_result(cancelled)
    assert not cancelled["acceptance_passed"]
    wrong_answer = {
        "mode": "normal",
        "output_cap": 64,
        "http_status": 200,
        "done": True,
        "finish_reason": "stop",
        "correct_response": False,
        "usage": {"completion_tokens": 10},
    }
    harness.assess_result(wrong_answer)
    assert not wrong_answer["acceptance_passed"]
