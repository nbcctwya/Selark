"""Paid Luna acceptance: budget limits, endpoint contracts and isolated smoke run."""

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from selark.connections import ConnectionConfig
from selark.providers.factory import provider_for
from selark.schemas import ChatRequest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "openai_luna_acceptance", SCRIPTS / "verify_openai_luna.py"
)
harness = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(harness)
finally:
    sys.path.remove(str(SCRIPTS))


def payload():
    return {
        "model": harness.MODEL,
        "messages": [{"role": "user", "content": "Test"}],
        "stream": False,
        "max_completion_tokens": 16,
        "store": False,
    }


@pytest.mark.parametrize("cost_limited", [False, True])
def test_concurrent_attempts_reserve_before_send_with_no_refund_or_directory_reset(
    tmp_path, cost_limited
):
    body = payload()
    charge = harness.reservation(
        len(httpx.Request("POST", harness.BASE_URL, json=body).content), 16
    )[1]
    guard = harness.LunaGuard(
        tmp_path,
        "fake-luna-key",
        maximum=30 if cost_limited else 7,
        budget=charge * 2 if cost_limited else 250000,
    )
    sent = []

    def upstream(request):
        saved = json.loads((tmp_path / "budget.json").read_text())
        assert saved["attempts"] > len(sent) and saved["reserved_micro_usd"] >= charge * (
            len(sent) + 1
        )
        sent.append(request)
        raise httpx.ReadTimeout("Invented timeout", request=request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(upstream),
            headers={"Authorization": "Bearer fake-luna-key"},
            event_hooks={"request": [guard.before]},
        ) as client:
            results = await asyncio.gather(
                *(
                    client.post(harness.BASE_URL + "/chat/completions", json=body)
                    for _ in range(30)
                ),
                return_exceptions=True,
            )
        limit = 2 if cost_limited else 7
        assert len(sent) == limit
        assert sum(isinstance(r, httpx.ReadTimeout) for r in results) == limit
        assert sum(isinstance(r, httpx.ConnectError) for r in results) == 30 - limit

    asyncio.run(run())
    with pytest.raises(FileExistsError):
        harness.LunaGuard(tmp_path, "fake-luna-key")


@pytest.mark.parametrize(
    "case",
    [
        "destination",
        "model",
        "cap",
        "input",
        "key",
        "hosted-tool",
        "function",
        "store",
        "tier",
        "stop",
    ],
)
def test_unapproved_paid_requests_do_not_reach_any_transport(tmp_path, case):
    guard = harness.LunaGuard(tmp_path, "fake-luna-key")
    body, url, key = payload(), harness.BASE_URL + "/chat/completions", "fake-luna-key"
    if case == "destination":
        url = "https://unexpected.invalid/v1/chat/completions"
    elif case == "model":
        body["model"] = "another-model"
    elif case == "cap":
        body["max_completion_tokens"] = 1025
    elif case == "input":
        body["messages"][0]["content"] = "x" * 100000
    elif case == "key":
        key = "incorrect"
    elif case == "hosted-tool":
        body["tools"] = [{"type": "web_search"}]
    elif case == "function":
        body["tools"] = [{"type": "function", "name": "unapproved_function"}]
    elif case == "store":
        body["store"] = True
    elif case == "tier":
        body["service_tier"] = "priority"
    elif case == "stop":
        (tmp_path / "STOP").touch()

    def forbidden(request):
        pytest.fail("Rejected request reached network transport")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden),
            headers={"Authorization": "Bearer " + key},
            event_hooks={"request": [guard.before]},
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(url, json=body)

    asyncio.run(run())
    assert not guard.events and not guard.reserved


@pytest.mark.parametrize("suite,expected_count", [("full", 158), ("followup", 8)])
def test_all_planned_payloads_fit_conservative_budget_and_gateway_supported_subset(
    suite, expected_count
):
    c = ConnectionConfig(
        connection_id="openai-luna",
        name="Fake",
        provider="openai",
        base_url=harness.BASE_URL,
        api_key="fake-luna-key",
        allowed_models=[harness.MODEL],
        default_model=harness.MODEL,
    )
    provider = provider_for(c, None)
    count = reserved = 0
    for phase, _, cases in harness.phases(suite):
        count += len(cases)
        for case in cases:
            body = case["payload"]
            if not case["direct"]:
                if phase == "gateway-option-boundaries":
                    continue
                body = provider.prepare(ChatRequest.model_validate(body), harness.MODEL)
            encoded = httpx.Request("POST", harness.BASE_URL, json=body).content
            assert len(encoded) <= 100000 and case["cap"] <= 1024
            if case["route"] == "chat" and case["stream"]:
                assert body["stream_options"] == {"include_usage": True}
            reserved += harness.reservation(len(encoded), case["cap"])[1]
    assert count == expected_count
    assert reserved < 220000  # Leaves space for the two tool-output follow-up histories.


def test_reasoning_only_limit_and_documented_rejection_are_distinct_from_wrong_answer(tmp_path):
    limited = {"cap": 8, "check": "limit"}
    result = {
        "http_status": 200,
        "done": True,
        "finish_reason": "length",
        "usage": {"completion_tokens": 8},
        "text": "",
    }
    harness.evaluate(limited, result)
    assert result["case_passed"]
    result["usage"]["completion_tokens"] = 9
    harness.evaluate(limited, result)
    assert not result["case_passed"]
    guard = harness.LunaGuard(tmp_path, "fake-luna-key")
    guard.observe(result | {"cap": 8})
    assert guard.stop_reason == "reported_output_exceeded_requested_cap"
    probe = {"cap": 512, "check": "capability", "expected": "ALPHA"}
    rejected = {"http_status": 400, "error_code": "unsupported_parameter"}
    harness.evaluate(probe, rejected)
    assert rejected["case_passed"] and rejected["capability_supported"] is False


def test_responses_usage_and_tool_arguments_are_checked_without_executing_generated_code():
    response = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "fake-id",
                "name": "add_numbers",
                "arguments": '{"a":17,"b":24}',
            }
        ],
        "usage": {
            "input_tokens": 15,
            "output_tokens": 12,
            "total_tokens": 27,
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    text, functions, usage, finish = harness.responses_final(response)
    result = {
        "text": text,
        "function_calls": functions,
        "usage": usage,
        "finish_reason": finish,
        "http_status": 200,
        "done": True,
    }
    case = {"cap": 512, "check": "tool", "expected": [{"a": 17, "b": 24}]}
    harness.evaluate(case, result)
    assert result["case_passed"] and usage["completion_tokens"] == 12
    result["function_calls"][0]["arguments"] = '{"a":17,"b":99}'
    harness.evaluate(case, result)
    assert not result["case_passed"]


@pytest.mark.parametrize(
    "blocked_by", ["spent", "attempts", "stopped", "spent-chain", "attempts-chain"]
)
def test_followup_does_not_reset_prior_budget_or_override_a_stop(tmp_path, monkeypatch, blocked_by):
    prior = {"attempts": 0, "reserved_micro_usd": 0, "stop_reason": None}
    report = {"completed": True}
    if blocked_by == "spent":
        prior["reserved_micro_usd"] = harness.BUDGET_MICRO_USD
    elif blocked_by == "attempts":
        prior["attempts"] = harness.MAX_REQUESTS - 1
    elif blocked_by == "spent-chain":
        report["prior_reserved_micro_usd"] = harness.BUDGET_MICRO_USD - 1
        prior["reserved_micro_usd"] = 1
    elif blocked_by == "attempts-chain":
        report["prior_generation_attempts"] = harness.MAX_REQUESTS - 8
        prior["attempts"] = 1
    else:
        prior["stop_reason"] = "insufficient_quota"
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(prior))
    path.with_name("report.json").write_text(json.dumps(report))
    monkeypatch.setattr(harness, "config_directory", lambda: tmp_path / "unused-source")
    args = argparse.Namespace(work_dir=tmp_path / "new", suite="followup", prior_budget=path)
    with pytest.raises(ValueError):
        asyncio.run(harness.run(args, "fake-luna-key"))
    assert not args.work_dir.exists()


def test_isolated_gateway_and_responses_smoke_use_only_mock_upstreams(tmp_path, monkeypatch):
    cases = [
        harness.make_case("gateway", expected="OK"),
        harness.make_case("responses", route="responses", stream=True, expected="OK"),
        harness.make_case("invalid", check="rejection", expected_status=400),
    ]
    cases[2]["payload"]["max_tokens"] = 0
    monkeypatch.setattr(harness, "phases", lambda suite="full": [("smoke", 2, cases)])
    monkeypatch.setattr(harness, "config_directory", lambda: tmp_path / "unused-source")
    upstream = []

    def reply(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": harness.MODEL})
        upstream.append(request)
        if request.url.path.endswith("/responses"):
            data = {
                "model": harness.MODEL,
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }
            events = [
                {"type": "response.output_text.delta", "delta": "OK"},
                {"type": "response.completed", "response": data},
            ]
            return httpx.Response(
                200,
                text="".join("data: " + json.dumps(e) + "\n\n" for e in events),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "fake-completion",
                "object": "chat.completion",
                "created": 1,
                "model": harness.MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    original_app = harness.create_app
    monkeypatch.setattr(
        harness,
        "create_app",
        lambda *a, **kw: original_app(*a, **kw, transport=httpx.MockTransport(reply)),
    )
    original_client = httpx.AsyncClient

    def client(*a, **kw):
        if "event_hooks" in kw:
            kw["transport"] = httpx.MockTransport(reply)
        return original_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    work = tmp_path / "work"
    work.mkdir()
    report = asyncio.run(harness.run(argparse.Namespace(work_dir=work), "fake-luna-key"))
    assert len(upstream) == 2 and report["outgoing_attempts"] == 2
    assert report["summary"]["case_passed"] == 3 and report["ledger_checks"]["passed"]
    assert (
        report["temporary_profiles_removed"] and not report["temporary_files_contain_provider_key"]
    )
