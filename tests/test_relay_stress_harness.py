"""Validate the paid relay investigation without opening any provider socket."""

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
spec = importlib.util.spec_from_file_location("relay_stress", SCRIPTS / "verify_relay_stress.py")
harness = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(harness)
finally:
    sys.path.remove(str(SCRIPTS))


def connection():
    return ConnectionConfig(
        connection_id="relay-test",
        name="Fake relay",
        provider="openai-compatible",
        base_url="https://relay.invalid/v1",
        api_key="fake-relay-stress-key",
        allowed_models=[f"fake-model-{i}" for i in range(8)],
        default_model="fake-model-0",
    )


def payload():
    return {
        "model": "fake-model-0",
        "messages": [{"role": "user", "content": "Test"}],
        "stream": False,
        "max_tokens": 8,
    }


def test_concurrent_failure_attempts_remain_reserved_and_run_cannot_be_reset(tmp_path):
    guard = harness.RelayGuard(tmp_path, connection(), maximum=7)
    calls = []

    def upstream(request):
        assert json.loads((tmp_path / "egress.json").read_text())["attempts"] > len(calls)
        calls.append(request)
        raise httpx.ReadTimeout("Invented timeout", request=request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(upstream),
            headers={"Authorization": "Bearer fake-relay-stress-key"},
            event_hooks={"request": [guard.before]},
        ) as client:
            results = await asyncio.gather(
                *(
                    client.post("https://relay.invalid/v1/chat/completions", json=payload())
                    for _ in range(30)
                ),
                return_exceptions=True,
            )
        assert sum(isinstance(r, httpx.ReadTimeout) for r in results) == 7
        assert sum(isinstance(r, httpx.ConnectError) for r in results) == 23

    asyncio.run(run())
    assert len(calls) == 7
    with pytest.raises(FileExistsError):
        harness.RelayGuard(tmp_path, connection())


@pytest.mark.parametrize("case", ["destination", "cap", "model", "key", "tools", "input", "stop"])
def test_unapproved_requests_cannot_reach_transport(tmp_path, case):
    guard = harness.RelayGuard(tmp_path, connection())
    body, url, key = payload(), "https://relay.invalid/v1/chat/completions", "fake-relay-stress-key"
    if case == "destination":
        url = "https://unapproved.invalid/v1/chat/completions"
    elif case == "cap":
        body["max_tokens"] = 1025
    elif case == "model":
        body["model"] = "not-approved"
    elif case == "key":
        key = "incorrect"
    elif case == "tools":
        body["tools"] = []
    elif case == "input":
        body["messages"][0]["content"] = "x" * 80_000
    elif case == "stop":
        (tmp_path / "STOP").touch()

    def forbidden(request):
        pytest.fail("Rejected request reached upstream")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden),
            headers={"Authorization": "Bearer " + key},
            event_hooks={"request": [guard.before]},
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(url, json=body)

    asyncio.run(run())
    assert not guard.events


@pytest.mark.parametrize("suite,expected_count", [("full", 246), ("controls", 40)])
def test_whole_plan_prepares_valid_payloads_and_direct_controls_match_gateway(
    suite, expected_count
):
    c = connection()
    phases = harness.build_phases(c.allowed_models, c.default_model, suite)
    assert sum(len(cases) for _, _, cases in phases) == expected_count
    assert max(n for _, n, _ in phases) == (64 if suite == "full" else 4)
    payloads = {}
    for phase, _, cases in phases:
        for case in cases:
            updates = {}
            if case["alias"] == "relay-completion":
                updates["token_parameter"] = "max_completion_tokens"
            if case["alias"].startswith("relay-native"):
                updates.update(
                    provider="anthropic",
                    anthropic_version="2023-06-01"
                    if case["alias"].endswith("old")
                    else "2026-10-21",
                )
            provider = provider_for(c.model_copy(update=updates), None)
            request = ChatRequest.model_validate(
                {
                    "model": case["model"],
                    "messages": case["messages"],
                    "max_tokens": case["cap"],
                    "stream": case["stream"],
                    **case.get("extra", {}),
                }
            )
            wire = provider.prepare(request, case["model"])
            assert len(json.dumps(wire).encode()) < 80_000
            assert wire.get("max_tokens", wire.get("max_completion_tokens")) <= 1024
            payloads.setdefault(phase, []).append(harness.digest(wire))
    if suite == "full":
        assert payloads["gateway-caps"] == payloads["direct-identical-caps"]


def test_cap_breach_is_failure_without_erasing_usage_or_treating_http_200_as_correct():
    case = {"cap": 8, "check": "limit"}
    result = {
        "http_status": 200,
        "done": True,
        "usage": {"completion_tokens": 974},
        "finish_reason": "stop",
    }
    harness.evaluate(case, result, "Unexpected long answer")
    assert result["transport_complete"] and not result["case_passed"]
    assert result["output_cap_respected"] is False
    assert result["usage"]["completion_tokens"] == 974
    result["usage"], result["finish_reason"] = {"completion_tokens": 8}, "length"
    harness.evaluate(case, result, "")
    assert result["case_passed"]


def test_error_headers_are_redacted_and_retry_after_is_respected(tmp_path):
    guard = harness.RelayGuard(tmp_path, connection())

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429, headers={"retry-after": "300", "x-request-id": "fake-relay-stress-key"}
                )
            ),
            headers={"Authorization": "Bearer fake-relay-stress-key"},
            event_hooks={"request": [guard.before], "response": [guard.after]},
        ) as client:
            response = await client.post(
                "https://relay.invalid/v1/chat/completions", json=payload()
            )
            assert response.status_code == 429
            with pytest.raises(httpx.ConnectError):
                await client.post("https://relay.invalid/v1/chat/completions", json=payload())

    asyncio.run(run())
    assert guard.stop_reason == "long_retry_after"
    assert "fake-relay-stress-key" not in (tmp_path / "egress.json").read_text()


def test_ledger_audit_excludes_ingress_overload_but_detects_lost_or_duplicate_attempts():
    calls = [
        {"name": "normal", "direct": False, "http_status": 200, "request_id": "r1"},
        {
            "name": "overload",
            "direct": False,
            "http_status": 429,
            "error_code": "server_overloaded",
            "request_id": "r2",
        },
        {"name": "cancel", "direct": False, "cancel": "before-headers"},
        {"name": "direct", "direct": True, "http_status": 200},
    ]
    rows = [
        {"request_id": "r1", "upstream_attempts": 1},
        {"request_id": "r3", "upstream_attempts": 1},
    ]
    audit = harness.audit_ledger(calls, rows, outgoing_attempts=3)
    assert audit["passed"] and audit["ingress_overloads"] == 1 and not audit["missing"]
    assert not harness.audit_ledger(calls, rows[1:], outgoing_attempts=3)["passed"]
    assert not harness.audit_ledger(calls, rows + rows[:1], outgoing_attempts=3)["passed"]
