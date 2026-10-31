"""Explicit relay investigation: protocol controls, content checks and bounded real load.

Uses only the configured relay, temporary Selark callers and a temporary ledger. Known
output-cap violations are recorded without stopping the investigation. No generation
retries, credential discovery, account operations or calls to other providers occur.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import tempfile
import time
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from verify_resource_matrix import serve, wait_for

from selark import __version__
from selark.app import create_app
from selark.config import _write_private, config_directory, initialize, load_config
from selark.connections import ConnectionsConfig
from selark.providers.factory import provider_for
from selark.providers.mimo import retry_after_seconds
from selark.runtime import Runtime
from selark.schemas import ChatRequest
from selark.storage import utc_now

MAX_REQUESTS = 256
MAX_SECONDS = 2400
MAX_OUTPUT = 1024


def digest(body):
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class RelayGuard:
    """Approve only the existing relay and reserve every attempted send durably."""

    def __init__(self, directory, connection, maximum=MAX_REQUESTS):
        self.directory, self.connection, self.maximum = directory, connection, maximum
        self.started = time.monotonic()
        self.events, self.requests = [], {}
        self.phase, self.stop_reason = None, None
        self.pause_until = 0
        self.failures_in_row = 0
        self.lock = asyncio.Lock()
        self.path = directory / "egress.json"
        with self.path.open("x"):
            pass
        os.chmod(self.path, 0o600)
        self.save()

    def save(self):
        data = {
            "maximum": self.maximum,
            "attempts": len(self.events),
            "stop_reason": self.stop_reason,
            "events": self.events,
        }
        encoded = json.dumps(data, ensure_ascii=False)
        encoded = encoded.replace(self.connection.api_key.get_secret_value(), "[redacted-key]")
        _write_private(self.path, json.loads(encoded))

    async def before(self, request):
        while time.monotonic() < self.pause_until:
            await asyncio.sleep(min(1, self.pause_until - time.monotonic()))
        async with self.lock:
            endpoint = str(request.url).removeprefix(self.connection.base_url)
            key = self.connection.api_key.get_secret_value()
            native = endpoint == "/messages"
            body = json.loads(request.content)
            cap = body.get("max_completion_tokens", body.get("max_tokens"))
            expected_header = key if native else "Bearer " + key
            if (
                request.method != "POST"
                or str(request.url)
                not in {
                    self.connection.base_url + "/chat/completions",
                    self.connection.base_url + "/messages",
                }
                or request.headers.get("x-api-key" if native else "authorization")
                != expected_header
                or body.get("model") not in self.connection.allowed_models
                or type(cap) is not int
                or not 1 <= cap <= MAX_OUTPUT
                or len(request.content) > 80_000
                or "tools" in body
                or len(self.events) >= self.maximum
                or time.monotonic() - self.started > MAX_SECONDS
                or self.stop_reason
                or (self.directory / "STOP").exists()
            ):
                raise httpx.ConnectError("Relay investigation stopped before send", request=request)
            event = {
                "number": len(self.events) + 1,
                "phase": self.phase,
                "endpoint": endpoint,
                "start_ms": round((time.monotonic() - self.started) * 1000, 2),
                "body_bytes": len(request.content),
                "wire_sha256": hashlib.sha256(request.content).hexdigest(),
                "payload_sha256": digest(body),
                "payload": body,
                "anthropic_version": request.headers.get("anthropic-version"),
            }
            self.events.append(event)
            self.requests[id(request)] = event
            self.save()

    async def after(self, response):
        event = self.requests[id(response.request)]
        event.update(
            headers_ms=round((time.monotonic() - self.started) * 1000, 2),
            http_status=response.status_code,
            headers={
                k: response.headers[k]
                for k in (
                    "content-type",
                    "content-encoding",
                    "x-request-id",
                    "x-oneapi-request-id",
                    "retry-after",
                )
                if k in response.headers
            },
        )
        if response.status_code in (401, 403):
            self.stop_reason = "relay_authorization_failed"
        if response.status_code == 429 or response.status_code >= 500:
            self.failures_in_row += 1
            delay = retry_after_seconds(response.headers.get("retry-after"))
            if delay is not None and delay > 120:
                self.stop_reason = "long_retry_after"
            else:
                self.pause_until = max(self.pause_until, time.monotonic() + (delay or 2))
            if self.failures_in_row >= 16:
                self.stop_reason = "sixteen_consecutive_upstream_errors"
        else:
            self.failures_in_row = 0
        self.save()


def build_phases(models, default, suite="full"):
    phases = []

    def case(name, **changes):
        result = {
            "name": name,
            "alias": "relay-max",
            "model": default,
            "stream": False,
            "cap": 128,
            "check": "exact",
            "expected": "RELAY_" + name,
            "direct": False,
        }
        result.update(changes)
        result.setdefault(
            "messages", [{"role": "user", "content": "Reply exactly with: " + result["expected"]}]
        )
        return result

    phases.append(
        (
            "baseline",
            1,
            [
                case(f"baseline-{i}", expected="RELAY_BASELINE_OK", stream=bool(i % 2))
                for i in range(4)
            ],
        )
    )
    limits = []
    numbers = [
        {"role": "user", "content": "Print the integers from 1 to 1000 separated by spaces."}
    ]
    for alias in ("relay-max", "relay-completion"):
        for stream in (False, True):
            for cap in (1, 8, 32, 128):
                limits.append(
                    case(
                        f"{alias}-{int(stream)}-{cap}",
                        alias=alias,
                        stream=stream,
                        cap=cap,
                        check="limit",
                        messages=numbers,
                    )
                )
    phases.append(("gateway-caps", 4, limits))
    phases.append(
        (
            "direct-identical-caps",
            4,
            [c | {"name": "direct-" + c["name"], "direct": True} for c in limits],
        )
    )
    native = []
    for alias in ("relay-native-old", "relay-native-current"):
        for stream in (False, True):
            for cap in (1, 8, 32, 128):
                native.append(
                    case(
                        f"{alias}-{int(stream)}-{cap}",
                        alias=alias,
                        stream=stream,
                        cap=cap,
                        check="limit",
                        messages=numbers,
                    )
                )
    phases.append(("native-messages-caps", 4, native))
    tasks = [
        (
            "unicode",
            [{"role": "user", "content": "只回复这六个字符：并发测试通过"}],
            "exact",
            "并发测试通过",
            {},
        ),
        (
            "arithmetic",
            [{"role": "user", "content": "17 * 24 = ? Reply only the integer."}],
            "exact",
            "408",
            {},
        ),
        (
            "json",
            [{"role": "user", "content": 'Return only JSON: {"ok":true,"items":[2,3,5]}'}],
            "json",
            '{"ok":true,"items":[2,3,5]}',
            {},
        ),
        (
            "sort",
            [{"role": "user", "content": "Sort [9,-2,9,0,5] ascending. Return only a JSON array."}],
            "json",
            "[-2,0,5,9,9]",
            {},
        ),
        (
            "history",
            [
                {"role": "user", "content": "Remember my code: amber-482."},
                {"role": "assistant", "content": "Understood."},
                {"role": "user", "content": "Reply only with my code."},
            ],
            "exact",
            "amber-482",
            {},
        ),
        (
            "system",
            [
                {
                    "role": "system",
                    "content": "For this formatting test, always reply exactly SYSTEM_OK.",
                },
                {"role": "user", "content": "Please answer now."},
            ],
            "exact",
            "SYSTEM_OK",
            {},
        ),
        (
            "stop",
            [{"role": "user", "content": "Reply exactly with: ALPHA<END>OMEGA"}],
            "exact",
            "ALPHA",
            {"stop": "<END>"},
        ),
        (
            "parts",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Reply exactly with: "},
                        {"type": "text", "text": "PARTS_OK"},
                    ],
                }
            ],
            "exact",
            "PARTS_OK",
            {},
        ),
    ]
    phases.append(
        (
            "content-and-options",
            4,
            [
                case(
                    f"{name}-{int(stream)}",
                    messages=messages,
                    check=check,
                    expected=expected,
                    extra=extra,
                    stream=stream,
                )
                for name, messages, check, expected, extra in tasks
                for stream in (False, True)
            ],
        )
    )
    contexts = []
    for length in (4096, 16384):
        padding = "reference=1234567890abcdef\n" * (length // 26)
        for stream in (False, True):
            contexts.append(
                case(
                    f"context-{length}-{int(stream)}",
                    messages=[
                        {
                            "role": "user",
                            "content": "Read this reference data.\nneedle=NEEDLE_482\n"
                            + padding
                            + "\nReply only with the value of needle.",
                        }
                    ],
                    stream=stream,
                    expected="NEEDLE_482",
                )
            )
    phases.append(("long-context", 4, contexts))
    phases.append(
        (
            "long-output",
            4,
            [
                case(
                    f"long-output-{i}",
                    alias="relay-max" if i < 2 else "relay-completion",
                    cap=1024,
                    stream=bool(i % 2),
                    check="limit",
                    messages=numbers,
                )
                for i in range(4)
            ],
        )
    )
    phases.append(
        ("slow-consumers", 4, [case(f"slow-{i}", stream=True, read_delay=0.03) for i in range(4)])
    )
    phases.append(
        (
            "all-configured-models",
            4,
            [
                case(f"model-{i}-{int(stream)}", model=model, stream=stream)
                for i, model in enumerate(models)
                for stream in (False, True)
            ],
        )
    )
    for concurrency in (1, 2, 4, 8, 16):
        phases.append(
            (
                f"ramp-{concurrency}",
                concurrency,
                [
                    case(f"ramp-{concurrency}-{i}", stream=bool(i % 2))
                    for i in range(concurrency * 2)
                ],
            )
        )
    phases.append(("burst-64", 64, [case(f"burst-{i}", stream=bool(i % 2)) for i in range(64)]))
    phases.append(
        (
            "cancel",
            8,
            [
                case(
                    f"cancel-{i}",
                    stream=True,
                    check="cancel",
                    cap=512,
                    messages=numbers,
                    cancel="before-headers" if i < 4 else "first-text",
                )
                for i in range(8)
            ],
        )
    )
    phases.append(("recovery", 8, [case(f"recovery-{i}", stream=bool(i % 2)) for i in range(8)]))
    phases.append(
        (
            "repeated-prompt",
            4,
            [
                case(f"repeat-{i}", expected="RELAY_BASELINE_OK", stream=bool(i % 2))
                for i in range(8)
            ],
        )
    )
    if suite == "controls":
        controls = []
        variants = (
            ("relay-max", False),
            ("relay-max", True),
            ("relay-native-old", False),
            ("relay-native-current", False),
        )
        for kind in ("system", "stop"):
            template = next(
                c
                for c in dict((name, cases) for name, _, cases in phases)["content-and-options"]
                if c["name"] == kind + "-0"
            )
            if kind == "stop":
                template = template | {
                    "messages": [
                        {"role": "user", "content": "Reply exactly with: ALPHA OMEGA TAIL"}
                    ],
                    "extra": {"stop": "OMEGA"},
                }
            controls.append(
                (
                    "control-" + kind,
                    4,
                    [
                        template
                        | {
                            "name": f"control-{kind}-{i}-{int(stream)}",
                            "alias": alias,
                            "direct": direct,
                            "stream": stream,
                        }
                        for i, (alias, direct) in enumerate(variants)
                        for stream in (False, True)
                    ],
                )
            )
        controls.append(
            (
                "control-arithmetic-models",
                4,
                [
                    case(
                        f"arithmetic-model-{i}-{int(stream)}",
                        model=model,
                        stream=stream,
                        expected="408",
                        messages=[
                            {
                                "role": "user",
                                "content": "What is 17 multiplied by 24? Reply only the integer.",
                            }
                        ],
                    )
                    for i, model in enumerate(models)
                    for stream in (False, True)
                ],
            )
        )
        controls.append(
            (
                "control-fresh-caps",
                4,
                [
                    c
                    | {
                        "name": "fresh-" + c["name"],
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    f"Print the integers from {i + 11} to {i + 111} "
                                    "separated by spaces."
                                ),
                            }
                        ],
                    }
                    for i, c in enumerate(c for c in limits if c["cap"] in (1, 8))
                ],
            )
        )
        phases = controls
    assert sum(len(cases) for _, _, cases in phases) <= MAX_REQUESTS
    return phases


def evaluate(case, result, text):
    usage = result.get("usage") or {}
    output = usage.get("completion_tokens")
    result["output_cap_respected"] = 0 <= output <= case["cap"] if type(output) is int else None
    result["transport_complete"] = bool(
        result.get("http_status") == 200
        and result.get("done")
        and not result.get("error_code")
        and not result.get("client_error_type")
    )
    if case["check"] == "limit":
        correct = result.get("finish_reason") == "length"
    elif case["check"] == "cancel":
        correct = result.get("intentionally_cancelled", False)
    elif case["check"] == "json":
        try:
            correct = json.loads(text) == json.loads(case["expected"])
        except (ValueError, TypeError):
            correct = False
    else:
        correct = text.strip() == case["expected"]
    result["content_check_passed"] = correct
    result["case_passed"] = bool(
        correct
        and (
            result["output_cap_respected"] is True
            or case["check"] == "cancel"
            and result["output_cap_respected"] is not False
        )
        and (result["transport_complete"] or case["check"] == "cancel")
    )


def summary(calls):
    return {
        "clients": len(calls),
        "http_statuses": dict(Counter(str(r.get("http_status")) for r in calls)),
        "complete": sum(r.get("transport_complete", False) for r in calls),
        "case_passed": sum(r.get("case_passed", False) for r in calls),
        "cap_violations": sum(r.get("output_cap_respected") is False for r in calls),
        "content_failures": sum(
            not r.get("content_check_passed", False)
            for r in calls
            if r.get("transport_complete") and r["check"] in {"exact", "json"}
        ),
        "cancelled": sum(r.get("intentionally_cancelled", False) for r in calls),
        "errors": dict(
            Counter(
                r.get("error_code") or r.get("client_error_type")
                for r in calls
                if r.get("error_code") or r.get("client_error_type")
            )
        ),
    }


def audit_ledger(calls, records, outgoing_attempts):
    """Ingress overload is logged before admission and intentionally has no ledger row."""
    rows = {row["request_id"]: row for row in records}
    expected, overloads = [], []
    for result in calls:
        if result["direct"] or result.get("cancel") == "before-headers":
            continue
        if result.get("http_status") == 429 and result.get("error_code") == "server_overloaded":
            overloads.append(result)
        else:
            expected.append(result)
    missing = [result["name"] for result in expected if result.get("request_id") not in rows]
    gateway_attempts = sum(row["upstream_attempts"] for row in records)
    direct_responses = sum(
        result["direct"] and result.get("http_status") is not None for result in calls
    )
    accounted = gateway_attempts + direct_responses == outgoing_attempts
    unique = len(rows) == len(records)
    no_retries = all(row["upstream_attempts"] in (0, 1) for row in records)
    return {
        "checked": len(expected) - len(missing),
        "ingress_overloads": len(overloads),
        "missing": missing,
        "gateway_attempts": gateway_attempts,
        "direct_responses": direct_responses,
        "all_attempts_accounted": accounted,
        "unique_records": unique,
        "no_retries": no_retries,
        "passed": not missing and accounted and unique and no_retries,
    }


async def run(args):
    source = config_directory(args.config_dir)
    paths = [source / name for name in ("server.json", "connections.json", "client.json")]
    originals = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    registry = load_config(source / "connections.json", ConnectionsConfig)
    relay = next(c for c in registry.connections if c.connection_id == args.connection)
    if not relay.enabled or relay.provider != "openai-compatible" or relay.resource_type != "api":
        raise ValueError("An enabled OpenAI-compatible API relay is required")
    phases = build_phases(relay.allowed_models, relay.default_model, args.suite)
    if sum(len(cases) for _, _, cases in phases) > args.max_requests:
        raise ValueError("The request budget must cover every planned client")
    key = relay.api_key.get_secret_value()
    guard = RelayGuard(args.work_dir, relay, maximum=args.max_requests)
    report = {
        "version": __version__,
        "started_at": utc_now(),
        "completed": False,
        "maximum_generation_attempts": args.max_requests,
        "suite": args.suite,
        "maximum_upstream_concurrency": 16,
        "maximum_queued": 16,
        "automatic_retries": 0,
        "other_provider_calls": 0,
        "continue_after_cap_violations": True,
        "models": relay.allowed_models,
        "planned_clients": sum(len(cases) for _, _, cases in phases),
        "calls": [],
        "phases": [],
    }

    def private_save():
        text = json.dumps(report, ensure_ascii=False)
        text = text.replace(key, "[redacted-key]").replace(relay.base_url, "[relay]")
        text = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}", "[redacted-key]", text)
        _write_private(args.work_dir / "report.json", json.loads(text))

    aliases = {}
    for identity, update in {
        "relay-max": {"token_parameter": "max_tokens"},
        "relay-completion": {"token_parameter": "max_completion_tokens"},
        "relay-native-old": {"provider": "anthropic", "anthropic_version": "2023-06-01"},
        "relay-native-current": {"provider": "anthropic", "anthropic_version": "2026-10-21"},
    }.items():
        aliases[identity] = relay.model_copy(
            update={"connection_id": identity, "timeout_seconds": 60.0, **update}
        )
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SELARK_") and not k.endswith(("_API_KEY", "_TOKEN"))
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        tempfile.TemporaryDirectory(prefix="selark-relay-stress-") as tmp,
    ):
        directory = Path(tmp)
        initialize(directory, "unused-relay-test-placeholder")
        with Runtime(directory) as runtime:
            runtime.connection_settings = lambda: list(aliases.values())
            config = runtime.settings()
            config.limits = config.limits.model_copy(
                update={
                    "max_concurrent_requests": 16,
                    "max_queued_requests": 16,
                    "queue_timeout_seconds": 75.0,
                    "request_timeout_seconds": 90.0,
                    "connect_timeout_seconds": 15.0,
                }
            )
            app = create_app(config, runtime=runtime)
            tokens = [
                runtime.store.create_token(
                    f"relay-test-app-{i}",
                    policy={"allowed_connections": list(aliases), "max_output_tokens": MAX_OUTPUT},
                )
                for i in range(16)
            ]
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(256)
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            try:
                async with serve(app, runtime, sock), AsyncExitStack() as stack:
                    app.state.http.event_hooks["request"].append(guard.before)
                    app.state.http.event_hooks["response"].append(guard.after)
                    clients = [
                        await stack.enter_async_context(
                            httpx.AsyncClient(
                                base_url=url,
                                trust_env=False,
                                timeout=100,
                                limits=httpx.Limits(max_connections=64, keepalive_expiry=0.5),
                                headers={"Authorization": "Bearer " + token["token"]},
                            )
                        )
                        for token in tokens
                    ]
                    direct = await stack.enter_async_context(
                        httpx.AsyncClient(
                            timeout=httpx.Timeout(60, connect=15),
                            follow_redirects=False,
                            limits=httpx.Limits(max_connections=16, keepalive_expiry=0.5),
                            event_hooks={"request": [guard.before], "response": [guard.after]},
                        )
                    )
                    monitor = await stack.enter_async_context(
                        httpx.AsyncClient(base_url=url, trust_env=False, timeout=5)
                    )

                    async def health():
                        response = await monitor.get("/health")
                        response.raise_for_status()
                        return response.json()

                    async def idle():
                        h = await health()
                        return h["capacity"]["admitted"] == h["storage"]["pending_records"] == 0

                    async def call(case, phase, index):
                        payload = {
                            "connection_id": case["alias"],
                            "model": case["model"],
                            "messages": case["messages"],
                            "max_tokens": case["cap"],
                            "stream": case["stream"],
                            "session_id": "relay-shared-session",
                            **case.get("extra", {}),
                        }
                        provider = provider_for(aliases[case["alias"]], None)
                        wire = provider.prepare(ChatRequest.model_validate(payload), case["model"])
                        result = {k: v for k, v in case.items() if k not in {"messages", "extra"}}
                        result.update(
                            phase=phase,
                            caller_index=index % len(clients),
                            expected_payload_sha256=digest(wire),
                        )
                        report["calls"].append(result)
                        client = direct if case["direct"] else clients[index % len(clients)]
                        target = (
                            relay.base_url + "/chat/completions"
                            if case["direct"]
                            else "/v1/chat/completions"
                        )
                        kwargs = (
                            {"json": wire, "headers": {"Authorization": "Bearer " + key}}
                            if case["direct"]
                            else {"json": payload}
                        )
                        text, usage, finish, done = "", None, None, False
                        started = time.monotonic()
                        try:
                            async with (
                                asyncio.timeout(
                                    1 if case.get("cancel") == "before-headers" else 100
                                ),
                                client.stream("POST", target, **kwargs) as response,
                            ):
                                result.update(
                                    http_status=response.status_code,
                                    headers_ms=round((time.monotonic() - started) * 1000, 2),
                                    request_id=response.headers.get("x-request-id"),
                                )
                                if response.status_code != 200:
                                    raw = (await response.aread()).decode(errors="replace")
                                    result["error_excerpt"] = raw[:2000]
                                    try:
                                        result["error_code"] = json.loads(raw).get("error", {}).get(
                                            "code"
                                        ) or "http_" + str(response.status_code)
                                    except (ValueError, AttributeError):
                                        result["error_code"] = "http_" + str(response.status_code)
                                elif case["stream"]:
                                    async for line in response.aiter_lines():
                                        if case.get("read_delay"):
                                            await asyncio.sleep(case["read_delay"])
                                        if not line.startswith("data:"):
                                            continue
                                        raw = line[5:].strip()
                                        if raw == "[DONE]":
                                            done = True
                                            break
                                        data = json.loads(raw)
                                        if "error" in data:
                                            result["error_code"] = (
                                                data["error"].get("code") or "sse_error"
                                            )
                                            result["error_excerpt"] = raw[:2000]
                                            break
                                        usage = data.get("usage") or usage
                                        result["returned_model"] = data.get("model") or result.get(
                                            "returned_model"
                                        )
                                        for choice in data.get("choices", []):
                                            piece = choice.get("delta", {}).get("content") or ""
                                            if piece and "first_text_ms" not in result:
                                                result["first_text_ms"] = round(
                                                    (time.monotonic() - started) * 1000, 2
                                                )
                                            text += piece
                                            finish = choice.get("finish_reason") or finish
                                        if text and case.get("cancel") == "first-text":
                                            result["intentionally_cancelled"] = True
                                            break
                                        if len(text) > 20_000:
                                            result["client_error_type"] = "ResponseTextLimit"
                                            break
                                else:
                                    data = json.loads(await response.aread())
                                    result["returned_model"] = data.get("model")
                                    result["response_id"] = data.get("id")
                                    if not data.get("choices"):
                                        result["error_code"] = "invalid_completion_structure"
                                        result["error_excerpt"] = json.dumps(
                                            data, ensure_ascii=False
                                        )[:2000]
                                    else:
                                        text = data["choices"][0]["message"].get("content") or ""
                                        finish = data["choices"][0].get("finish_reason")
                                        usage, done = data.get("usage"), True
                        except TimeoutError:
                            if case.get("cancel") == "before-headers":
                                result["intentionally_cancelled"] = True
                            else:
                                result["client_error_type"] = "TotalTimeout"
                        except Exception as exc:
                            result["client_error_type"] = type(exc).__name__
                        result.update(
                            duration_ms=round((time.monotonic() - started) * 1000, 2),
                            text=text[:20_000],
                            text_characters=len(text),
                            usage=usage,
                            finish_reason=finish,
                            done=done,
                        )
                        evaluate(case, result, text)
                        private_save()
                        print(
                            json.dumps(
                                {
                                    k: result.get(k)
                                    for k in (
                                        "name",
                                        "phase",
                                        "http_status",
                                        "duration_ms",
                                        "case_passed",
                                        "output_cap_respected",
                                        "error_code",
                                        "client_error_type",
                                    )
                                }
                            ),
                            flush=True,
                        )
                        return result

                    for name, concurrency, cases in phases:
                        if guard.stop_reason or (args.work_dir / "STOP").exists():
                            report["stopped_before_phase"] = name
                            break
                        guard.phase = name
                        gate = asyncio.Semaphore(concurrency)
                        info = {
                            "name": name,
                            "concurrent_clients": concurrency,
                            "planned": len(cases),
                            "peak_active": 0,
                            "peak_queued": 0,
                            "health_checks": 0,
                            "health_errors": 0,
                            "max_health_ms": 0,
                        }
                        report["phases"].append(info)
                        start = time.monotonic()

                        async def work(case, index, gate=gate, phase=name):
                            async with gate:
                                return await call(case, phase, index)

                        jobs = [asyncio.create_task(work(case, i)) for i, case in enumerate(cases)]
                        try:
                            while not all(job.done() for job in jobs):
                                before = time.monotonic()
                                try:
                                    h = await health()
                                    info["peak_active"] = max(
                                        info["peak_active"], h["capacity"]["active"]
                                    )
                                    info["peak_queued"] = max(
                                        info["peak_queued"], h["capacity"]["queued"]
                                    )
                                    assert (
                                        h["capacity"]["active"] <= 16
                                        and h["capacity"]["queued"] <= 16
                                    )
                                    info["health_checks"] += 1
                                except httpx.HTTPError:
                                    info["health_errors"] += 1
                                info["max_health_ms"] = max(
                                    info["max_health_ms"],
                                    round((time.monotonic() - before) * 1000, 2),
                                )
                                await asyncio.sleep(0.1)
                        finally:
                            results = await asyncio.gather(*jobs, return_exceptions=True)
                        failures = [
                            type(r).__name__ for r in results if isinstance(r, BaseException)
                        ]
                        if failures:
                            info["harness_exceptions"] = failures
                            raise RuntimeError("Relay harness task failed")
                        await wait_for(idle)
                        info.update(
                            summary(results),
                            duration_ms=round((time.monotonic() - start) * 1000, 2),
                            outgoing_so_far=len(guard.events),
                        )
                        private_save()
                        print("PHASE " + json.dumps(info), flush=True)

                    report["final_health"] = await health()
                    report["records"] = runtime.store.records({}, limit=1000)
                    rows = {r["request_id"]: r for r in report["records"]}
                    checked = 0
                    for result in report["calls"]:
                        if result["direct"] or result.get("cancel") == "before-headers":
                            continue
                        row = rows.get(result.get("request_id"))
                        if row is None:
                            continue
                        assert row["caller_id"] == tokens[result["caller_index"]]["id"]
                        assert row["connection_id"] in (None, result["alias"])
                        assert row["upstream_attempts"] in (0, 1)
                        if result["done"] and result["usage"]:
                            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                assert row[field] == result["usage"].get(field)
                        checked += 1
                    report["ledger_checks"] = audit_ledger(
                        report["calls"], report["records"], len(guard.events)
                    ) | {"caller_bindings_checked": checked}
                report["recovery"] = runtime.recover_records()
                assert not any(report["recovery"].values())
                report["completed"] = True
            except Exception as exc:
                report["harness_failure_type"] = type(exc).__name__
                raise
            finally:
                report["outgoing_attempts"] = len(guard.events)
                report["stop_reason"] = guard.stop_reason
                report["summary"] = summary(report["calls"])
                report["source_configuration_unchanged"] = all(
                    hashlib.sha256(p.read_bytes()).hexdigest() == value
                    for p, value in originals.items()
                )
                report["temporary_files_contain_provider_key"] = any(
                    key.encode() in p.read_bytes() for p in directory.rglob("*") if p.is_file()
                )
                assert not report["temporary_files_contain_provider_key"]
                private_save()
    report["temporary_profiles_removed"] = True
    report["finished_at"] = utc_now()
    private_save()
    print(
        "FINAL "
        + json.dumps(
            report["summary"]
            | {"outgoing_attempts": len(guard.events), "completed": report["completed"]}
        ),
        flush=True,
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--connection", default="relay-personal")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--suite", choices=("full", "controls"), default="full")
    parser.add_argument(
        "--max-requests", type=int, choices=range(1, MAX_REQUESTS + 1), default=MAX_REQUESTS
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute or os.environ.get("SELARK_DATA_DIR"):
        parser.error("Use --execute and unset SELARK_DATA_DIR for isolated paid investigation")
    args.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    asyncio.run(run(args))
