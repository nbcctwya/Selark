"""MiMo acceptance spending and protocol checks; all upstreams are simulated."""

import argparse
import asyncio
import importlib.util
import io
import json
import sys
import wave
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    spec = importlib.util.spec_from_file_location(
        "mimo_extended_acceptance", SCRIPTS / "verify_mimo_extended.py"
    )
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
finally:
    sys.path.remove(str(SCRIPTS))


def test_concurrent_requests_cannot_overspend_or_refund_failed_attempts(tmp_path):
    payload = harness.case("test")["payload"]
    amount = harness.reservation(payload)
    guard = harness.MiMoGuard(tmp_path, "fake-mimo-key", maximum=amount * 5)
    sent = []

    def upstream(request):
        budget = json.loads((tmp_path / "budget.json").read_text())
        assert budget["attempts"] > len(sent)
        assert budget["reserved_micros"] >= amount * (len(sent) + 1)
        sent.append(request)
        raise httpx.ReadTimeout("invented timeout", request=request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(upstream),
            headers={"api-key": "fake-mimo-key"},
            event_hooks={"request": [guard.before]},
        ) as client:
            results = await asyncio.gather(
                *(client.post(harness.ENDPOINTS["chat"], json=payload) for _ in range(20)),
                return_exceptions=True,
            )
        assert sum(isinstance(r, httpx.ReadTimeout) for r in results) == 5
        assert sum(isinstance(r, httpx.ConnectError) for r in results) == 15

    asyncio.run(run())
    assert guard.reserved == 5 * amount and len(sent) == 5
    with pytest.raises(FileExistsError):
        harness.MiMoGuard(tmp_path, "fake-mimo-key")


@pytest.mark.parametrize(
    "invalid", ["model", "cap", "store", "tool", "remote-image", "large-input"]
)
def test_unbounded_payloads_are_rejected(invalid):
    payload = harness.case("test")["payload"]
    if invalid == "model":
        payload["model"] = "unapproved"
    elif invalid == "cap":
        payload["max_completion_tokens"] = 16385
    elif invalid == "store":
        payload["store"] = True
    elif invalid == "tool":
        payload["tools"] = [{"type": "function", "function": {"name": "run_shell"}}]
    elif invalid == "remote-image":
        payload["messages"][0]["content"] = [
            {"type": "image_url", "image_url": {"url": "https://invalid.test/image"}}
        ]
    else:
        payload["messages"][0]["content"] = "x" * 1_100_001
    with pytest.raises(ValueError):
        harness.reservation(payload)


@pytest.mark.parametrize("invalid", ["origin", "key", "stop"])
def test_egress_binding_and_stop_before_send(tmp_path, invalid):
    guard = harness.MiMoGuard(tmp_path, "fake-mimo-key")
    url, key = harness.ENDPOINTS["chat"], "fake-mimo-key"
    if invalid == "origin":
        url = "https://invalid.test/v1/chat/completions"
    elif invalid == "key":
        key = "wrong-key"
    else:
        (tmp_path / "STOP").touch()
    request = httpx.Request(
        "POST", url, headers={"api-key": key}, json=harness.case("test")["payload"]
    )
    with pytest.raises(httpx.ConnectError):
        asyncio.run(guard.before(request))
    assert guard.reserved == 0 and not guard.events


def test_static_matrix_and_inline_media_fit_total_budget(tmp_path):
    cases = [c for _, _, cs in harness.static_phases() for c in cs] + harness.vision_cases(tmp_path)
    amounts = [harness.reservation(c["payload"]) for c in cases]
    assert len(cases) == 185 and sum(amounts) < 11_000_000
    assert len({c["name"] for c in cases}) == len(cases)


def test_targeted_controls_and_fixed_calculations(tmp_path):
    cases = harness.focused_cases(tmp_path)
    assert len(cases) == 22
    assert sum(harness.reservation(c["payload"]) for c in cases) < 2_200_000
    assert harness.calculate_fixture("sales-240") == {"A": 3215, "B": 3936, "C": 4647}
    brute_force = next(
        c["expected"]
        for _, _, cs in harness.static_phases()
        for c in cs
        if c["name"].startswith("knapsack-")
    )
    assert harness.calculate_fixture("knapsack-12") == brute_force == {"maximum_value": 130}
    with pytest.raises(ValueError):
        harness.calculate_fixture("unapproved")


def test_speech_workflow_keeps_synthesis_recognition_and_understanding_together(
    tmp_path, monkeypatch
):
    guard = harness.MiMoGuard(tmp_path, "fake-mimo-key")
    runner = harness.Runner(tmp_path, guard)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 8000)
    (tmp_path / "fixture.wav").write_bytes(buffer.getvalue())
    calls = []

    async def call(c):
        harness.reservation(c["payload"])
        calls.append(c["name"])
        return {"passed": True, "audio_file": "fixture.wav", "audio_seconds": 1}

    monkeypatch.setattr(runner, "call", call)
    asyncio.run(runner.audio_workflows())
    assert calls == [
        "tts-zh",
        "asr-zh",
        "audio-understanding-zh",
        "tts-en",
        "asr-en",
        "audio-understanding-en",
    ]


def test_calculator_workflow_executes_only_validated_fixed_datasets(tmp_path, monkeypatch):
    runner = harness.Runner(tmp_path, harness.MiMoGuard(tmp_path, "fake-mimo-key"))
    calls = []

    async def call(c):
        harness.reservation(c["payload"])
        calls.append(c)
        if c["check"] == "fixture_tool":
            return {
                "passed": True,
                "text": "",
                "function_calls": [
                    {
                        "id": "fake-call",
                        "name": "calculate_fixture",
                        "arguments": json.dumps({"dataset": c["expected"]}),
                    }
                ],
            }
        return {"passed": True}

    monkeypatch.setattr(runner, "call", call)
    asyncio.run(runner.focused_workflows())
    assert len(calls) == 10
    following = [c for c in calls if c["name"].endswith("-followup")]
    assert len(following) == 4
    for c in following:
        assert json.loads(c["payload"]["messages"][-1]["content"]) == c["expected"]


def test_chat_stream_preserves_reasoning_tools_and_final_usage():
    c = harness.Collector("chat")
    c.feed(
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "reason",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "fake-call",
                                "function": {"name": "add_numbers", "arguments": '{"a":17,'},
                            }
                        ],
                    }
                }
            ]
        },
        stream=True,
    )
    c.feed(
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"b":24}'}}]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
        },
        stream=True,
    )
    r = c.result()
    assert r["reasoning_content"] == "reason"
    assert json.loads(r["function_calls"][0]["arguments"]) == {"a": 17, "b": 24}
    assert r["usage"]["completion_tokens"] == 15


def test_nullable_tool_calls_and_usage_only_chunks_are_valid():
    c = harness.Collector("chat")
    c.feed({"choices": [{"delta": {"content": "READY", "tool_calls": None}}]}, stream=True)
    c.feed(
        {"choices": None, "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        stream=True,
    )
    assert c.result()["text"] == "READY" and c.result()["usage"]["total_tokens"] == 5
    c = harness.Collector("chat")
    c.feed(
        {
            "choices": [
                {"message": {"content": "READY", "tool_calls": None}, "finish_reason": "stop"}
            ]
        },
        stream=False,
    )
    assert c.done and not c.result()["function_calls"]
    c = harness.Collector("chat")
    c.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "fake-call",
                                "function": {"name": "add_numbers", "arguments": None},
                            }
                        ]
                    }
                }
            ]
        },
        stream=True,
    )
    c.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": None, "arguments": '{"a":17,"b":24}'}}
                        ]
                    }
                }
            ]
        },
        stream=True,
    )
    assert c.result()["function_calls"][0]["name"] == "add_numbers"
    assert json.loads(c.result()["function_calls"][0]["arguments"])["b"] == 24


def test_anthropic_stream_combines_cached_input_and_output_counts():
    c = harness.Collector("anthropic")
    c.feed(
        {
            "type": "message_start",
            "message": {
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 0}
            },
        },
        stream=True,
    )
    c.feed({"type": "content_block_delta", "delta": {"text": "READY"}}, stream=True)
    c.feed(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        stream=True,
    )
    c.feed({"type": "message_stop"}, stream=True)
    r = c.result()
    assert r["text"] == "READY" and r["done"]
    assert r["usage"]["total_tokens"] == 32


def test_nested_search_usage_is_retained():
    c = harness.Collector("chat")
    c.feed(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "web_search_usage": {"tool_usage": 1, "page_usage": 3},
            },
        },
        stream=True,
    )
    assert c.result()["web_search_usage"] == {"tool_usage": 1, "page_usage": 3}


def test_strict_answer_checks_do_not_confuse_http_success_with_correctness():
    c = harness.case("json", check="json", expected={"a": 1})
    r = {
        "http_status": 200,
        "done": True,
        "text": '```json\n{"a":1}\n```',
        "usage": {"completion_tokens": 12},
    }
    harness.evaluate(c, r)
    assert not r["passed"] and r["cap_respected"]
    r["text"] = '{"a":1}'
    harness.evaluate(c, r)
    assert r["passed"]
    r["usage"]["completion_tokens"] = c["cap"] + 1
    harness.evaluate(c, r)
    assert not r["passed"]


@pytest.mark.parametrize("reason", ["cost-chain", "attempt-chain", "quota"])
def test_continuation_cannot_reset_cumulative_spending_or_quota_stop(tmp_path, monkeypatch, reason):
    from selark.config import initialize

    original = tmp_path / "original"
    initialize(original, "fake-mimo-key")
    monkeypatch.setattr(harness, "config_directory", lambda: original)
    prior = tmp_path / "prior"
    prior.mkdir()
    report = {"temporary_profiles_removed": True}
    budget = {"attempts": 0, "reserved_micros": 0, "stop_reason": None}
    if reason == "cost-chain":
        report["prior_reserved_micros"] = harness.MAX_MICROS - 1
        budget["reserved_micros"] = 1
    elif reason == "attempt-chain":
        report["prior_attempts"] = harness.MAX_ATTEMPTS - 1
        budget["attempts"] = 1
    else:
        budget["stop_reason"] = "insufficient_balance"
    (prior / "report.json").write_text(json.dumps(report))
    (prior / "budget.json").write_text(json.dumps(budget))
    work = tmp_path / "new"
    with pytest.raises(ValueError):
        asyncio.run(harness.run(argparse.Namespace(work_dir=work, prior_run=prior)))
    assert not work.exists()


def test_isolated_runtime_makes_only_mock_upstream_calls(tmp_path, monkeypatch):
    from selark.config import initialize

    original = tmp_path / "original"
    initialize(original, "fake-mimo-key")
    monkeypatch.setattr(harness, "config_directory", lambda: original)
    monkeypatch.setattr(
        harness,
        "static_phases",
        lambda: [("smoke", 2, [harness.case("gateway", route="gateway"), harness.case("direct")])],
    )
    monkeypatch.setattr(harness, "vision_cases", lambda root: [])

    async def nothing(self):
        pass

    for method in ("audio_workflows", "tool_workflows", "conversations"):
        monkeypatch.setattr(harness.Runner, method, nothing)
    calls = []

    def reply(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": harness.FLASH}]})
        calls.append(request)
        body = json.loads(request.content)
        data = {
            "model": body["model"],
            "id": "fake-id",
            "object": "chat.completion",
            "created": 1,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "READY"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        if body.get("stream"):
            data["choices"][0]["delta"] = data["choices"][0].pop("message")
            return httpx.Response(
                200,
                text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=data)

    original_app, original_client = harness.create_app, httpx.AsyncClient
    monkeypatch.setattr(
        harness,
        "create_app",
        lambda *a, **kw: original_app(*a, **kw, transport=httpx.MockTransport(reply)),
    )

    def client(*a, **kw):
        if "event_hooks" in kw:
            kw["transport"] = httpx.MockTransport(reply)
        return original_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    report = asyncio.run(harness.run(argparse.Namespace(work_dir=tmp_path / "run")))
    assert report["completed"] and report["temporary_profiles_removed"]
    assert report["source_configuration_unchanged"]
    assert not report["temporary_files_contain_provider_key"]
    assert report["final_health"]["capacity"]["admitted"] == 0
    rows = {row["request_id"] for row in report["records"]}
    assert all(
        r["request_id"] in rows for r in report["calls"] if r["phase"] == "gateway-unsupported"
    )
    assert report["outgoing_attempts"] == len(calls) == 4
    assert all(r["passed"] for r in report["calls"] if r["phase"] == "smoke")
    assert all(r["passed"] for r in report["calls"] if r["phase"] == "gateway-unsupported")
