"""Independent applications over real loopback HTTP, using invented upstreams only.

No real profiles, provider sockets, API keys, subscriptions or OAuth are used.
Run with the installed Selark Python to exercise that installation.
"""

import argparse
import asyncio
import json
import math
import os
import socket
import sys
import tempfile
import time
from collections import Counter
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from verify_resource_matrix import serve, wait_for

from selark import __version__
from selark.governance import estimate_cost, input_estimate
from selark.schemas import ChatRequest
from selark.storage import utc_now

ROOT = Path(__file__).resolve().parents[1]
APPS = {
    "chat-desktop": ("mimo-default", 2),
    "ide-helper": ("deepseek-test", 2),
    "notes-summary": ("mimo-default", 1),
    "translation-worker": ("relay-test", 2),
    "batch-indexer": ("relay-test", 1),
    "daily-report": ("mimo-default", 2),
}
USAGE = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def percentiles(values):
    ordered = sorted(values)
    return {
        name: round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 2)
        for name, percentile in (("p50", 0.5), ("p95", 0.95), ("max", 1))
    }


class Stream(httpx.AsyncByteStream):
    def __init__(self, owner, identity, model, tag, mode):
        self.owner, self.model, self.tag, self.mode = owner, model, tag, mode
        self.identity = identity
        self.closed = False

    async def __aiter__(self):
        def frame(content=None, finish=None, usage=None):
            return (
                "data: "
                + json.dumps(
                    {
                        "id": self.tag,
                        "object": "chat.completion.chunk",
                        "model": self.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {} if content is None else {"content": content},
                                "finish_reason": finish,
                            }
                        ],
                        **({"usage": usage} if usage is not None else {}),
                    }
                )
                + "\n\n"
            ).encode()

        middle = len(self.tag) // 2
        yield frame(self.tag[:middle])
        if self.mode == "disconnect":
            await asyncio.Event().wait()
        await asyncio.sleep(0.015)
        yield frame(self.tag[middle:])
        if self.mode == "truncated":
            return
        await asyncio.sleep(0.015)
        yield frame(finish="stop", usage=None if self.mode == "unknown" else USAGE)
        if self.mode != "unknown":
            yield frame(finish="stop", usage=USAGE)  # Cumulative usage is not added twice.
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        if not self.closed:
            self.closed = True
            self.owner.release(self.identity)


class Upstreams:
    def __init__(self, resources, apps):
        self.resources = resources
        self.apps = apps
        self.calls = []
        self.active = self.peak = 0
        self.active_by_connection = Counter()
        self.peak_by_connection = Counter()
        self.peak_distinct_connections = 0
        self.hold = asyncio.Event()

    def release(self, identity):
        self.active -= 1
        self.active_by_connection[identity] -= 1

    async def __call__(self, request):
        identity = next(
            (k for k, (_, _, host) in self.resources.items() if host == request.url.host), None
        )
        assert identity in {connection for connection, _ in self.apps.values()}
        assert request.method == "POST" and request.url.path.endswith("/chat/completions")
        data = json.loads(request.content)
        assert not {"session_id", "caller_id", "connection_id"} & data.keys()
        task = json.loads(data["messages"][-1]["content"])
        assert self.apps[task["application"]][0] == identity
        provider = self.resources[identity][0]
        if identity == "mimo-default":
            assert request.headers["api-key"] == "fake-matrix-mimo"
        else:
            assert request.headers["authorization"] == "Bearer fake-matrix-" + provider
        parameter = "max_completion_tokens" if provider in {"mimo", "openai"} else "max_tokens"
        assert data[parameter] == 16
        assert ("thinking" in data) == (provider in {"mimo", "deepseek"})
        if data["stream"] and provider != "mimo":
            assert data["stream_options"]["include_usage"] is True
        tag = task["application"] + ":" + task["case"]
        mode = task["mode"]
        self.calls.append(
            {"application": task["application"], "case": task["case"], "connection": identity}
        )
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.active_by_connection[identity] += 1
        self.peak_by_connection[identity] = max(
            self.peak_by_connection[identity], self.active_by_connection[identity]
        )
        self.peak_distinct_connections = max(
            self.peak_distinct_connections,
            sum(value > 0 for value in self.active_by_connection.values()),
        )
        handed_off = False
        try:
            if mode == "hold":
                await self.hold.wait()
            if mode == "rate":
                return httpx.Response(429, headers={"Retry-After": "2"}, text="fake limit")
            if mode == "auth":
                return httpx.Response(401, text="fake authorization failure")
            if mode == "server-error":
                return httpx.Response(503, text="fake provider failure")
            if mode == "timeout":
                raise httpx.ReadTimeout("invented timeout; no provider socket")
            if mode == "malformed":
                return httpx.Response(200, content=b"invalid-json")
            if data["stream"]:
                handed_off = True
                return httpx.Response(
                    200,
                    stream=Stream(self, identity, data["model"], tag, mode),
                    headers={"Content-Type": "text/event-stream"},
                )
            await asyncio.sleep(0.025)
            return httpx.Response(
                200,
                json={
                    "id": tag,
                    "object": "chat.completion",
                    "model": data["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": tag},
                            "finish_reason": "stop",
                        }
                    ],
                    **({"usage": USAGE} if mode != "unknown" else {}),
                },
            )
        finally:
            if not handed_off:
                self.release(identity)


async def run(args):
    sys.path.insert(0, str(ROOT))
    from tests.four_resources import RESOURCES, app_for, configure

    apps, resources = dict(APPS), dict(RESOURCES)
    if args.four_api:
        resources["openai-test"] = ("openai", "gpt-6-luna", "api.openai.com")
        apps.update({"openai-chat": ("openai-test", 2), "openai-review": ("openai-test", 2)})
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SELARK_") and not k.endswith(("_API_KEY", "_TOKEN"))
    }
    report = {
        "version": __version__,
        "verified_at": utc_now(),
        "scope": "independent tokens and HTTP pools; loopback TCP; fake upstream only",
        "four_api": args.four_api,
        "real_generation_requests": 0,
        "real_provider_metadata_requests": 0,
        "oauth_operations": 0,
        "checks": [],
    }
    checks, outcomes = report["checks"], []

    def check(name, passed, **details):
        checks.append({"check": name, "passed": bool(passed), **details})
        assert passed, name

    with (
        patch.dict(os.environ, environment, clear=True),
        tempfile.TemporaryDirectory(prefix="selark-six-apps-") as tmp,
    ):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        with configure(Path(tmp) / "profile", port=sock.getsockname()[1]) as runtime:
            if args.four_api:
                runtime.add_connection(
                    "openai-test",
                    "openai",
                    "fake-matrix-openai",
                    allowed_models=[resources["openai-test"][1]],
                )
            upstream = Upstreams(resources, apps)
            tokens = {
                name: runtime.store.create_token(
                    name,
                    policy={
                        "allowed_connections": [connection],
                        "allowed_models": [connection + "::" + resources[connection][1]],
                        "max_concurrent_requests": None,
                    },
                )
                for name, (connection, limit) in apps.items()
            }
            prices = {}
            for connection in {value[0] for value in apps.values()}:
                model = resources[connection][1]
                prices[connection] = {
                    "connection_id": connection,
                    "model": model,
                    "currency": "CNY",
                    "input_per_million": "1",
                    "output_per_million": "2",
                    "note": "Invented simulation tariff, not a provider quote",
                }
                runtime.edit_resource("price", (connection, model), prices[connection])
            app = app_for(
                runtime,
                upstream,
                max_concurrent_requests=4,
                max_queued_requests=8,
                queue_timeout_seconds=4.0,
                request_timeout_seconds=8.0,
            )
            try:
                async with serve(app, runtime, sock), AsyncExitStack() as stack:
                    clients = {
                        name: await stack.enter_async_context(
                            httpx.AsyncClient(
                                base_url=url,
                                headers={"Authorization": "Bearer " + token["token"]},
                                trust_env=False,
                                timeout=10,
                                limits=httpx.Limits(
                                    max_connections=max(20, args.burst_clients),
                                    keepalive_expiry=0.5,
                                ),
                            )
                        )
                        for name, token in tokens.items()
                    }
                    monitor = await stack.enter_async_context(
                        httpx.AsyncClient(base_url=url, trust_env=False, timeout=10)
                    )

                    async def health():
                        response = await monitor.get("/health")
                        assert response.status_code == 200
                        return response.json()

                    async def idle():
                        h = await health()
                        return h["capacity"]["admitted"] == h["storage"]["pending_records"] == 0

                    def payload(name, case, mode="fast", stream=False):
                        return {
                            "connection_id": apps[name][0],
                            "model": resources[apps[name][0]][1],
                            "messages": [
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {"application": name, "case": case, "mode": mode}
                                    ),
                                }
                            ],
                            "max_tokens": 16,
                            "stream": stream,
                            "session_id": "shared-project" if case.startswith("steady-") else case,
                        }

                    async def call(name, case, *, mode="fast", stream=False, overrides=None):
                        start = time.monotonic()
                        text, error, done, usage = "", None, False, None
                        async with clients[name].stream(
                            "POST",
                            "/v1/chat/completions",
                            json=payload(name, case, mode, stream) | (overrides or {}),
                        ) as response:
                            if response.status_code != 200:
                                data = json.loads(await response.aread())
                                error = data["error"]["code"]
                            elif not stream:
                                data = json.loads(await response.aread())
                                text = data["choices"][0]["message"]["content"]
                                usage, done = data.get("usage"), True
                            else:
                                async for line in response.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    body = line[5:].strip()
                                    if body == "[DONE]":
                                        done = True
                                        break
                                    event = json.loads(body)
                                    if "error" in event:
                                        error = event["error"]["code"]
                                        break
                                    usage = event.get("usage") or usage
                                    for choice in event.get("choices", []):
                                        text += choice["delta"].get("content", "")
                                    if mode == "disconnect" and text:
                                        break
                            outcome = {
                                "application": name,
                                "case": case,
                                "mode": mode,
                                "stream": stream,
                                "request_id": response.headers.get("x-request-id"),
                                "status": response.status_code,
                                "error": error,
                                "retry_after": response.headers.get("retry-after"),
                                "done": done,
                                "correct_response": text == name + ":" + case,
                                "usage": usage,
                                "duration_ms": round((time.monotonic() - start) * 1000, 2),
                            }
                            outcomes.append(outcome)
                            return outcome

                    async def worker(name):
                        for i in range(args.requests_per_app):
                            result = await call(name, f"steady-{i:04}", stream=bool(i % 2))
                            assert result["done"] and result["correct_response"], (
                                name,
                                result["case"],
                                result["status"],
                                result["error"],
                            )

                    samples, watching = [], True

                    async def watch():
                        while watching:
                            start = time.monotonic()
                            h = await health()
                            samples.append((time.monotonic() - start) * 1000)
                            assert h["capacity"]["active"] <= 4
                            assert h["capacity"]["queued"] <= 8
                            await asyncio.sleep(0.025)

                    watcher = asyncio.create_task(watch())
                    try:
                        workers = await asyncio.gather(
                            *(worker(name) for name in apps), return_exceptions=True
                        )
                        failures = [
                            {"application": name, "exception": type(result).__name__}
                            for name, result in zip(apps, workers, strict=True)
                            if isinstance(result, BaseException)
                        ]
                        if failures:
                            report["worker_failures"] = failures
                            raise AssertionError("Normal application traffic failed; see outcomes")
                        await wait_for(idle)
                    finally:
                        watching = False
                        await watcher
                    groups = runtime.store.sessions({"session_id": "shared-project"})["groups"]
                    check(
                        "applications_same_session_isolated",
                        len(groups) == len(apps)
                        and all(
                            g["known_tokens"]["total_tokens"] == 14 * args.requests_per_app
                            for g in groups
                        ),
                        requests=len(apps) * args.requests_per_app,
                        health_samples=len(samples),
                        health_latency_ms=percentiles(samples),
                    )

                    # First isolate global queue behavior from per-application policies.
                    for token in tokens.values():
                        runtime.store.set_policy(token["id"], {"max_concurrent_requests": None})
                    names = list(apps)

                    async def four_active():
                        return upstream.active == 4

                    async def burst(label, selected):
                        upstream.hold.clear()
                        distinct = {}
                        for name in selected:
                            distinct.setdefault(apps[name][0], name)
                        starters = list(distinct.values())
                        starters += [selected[i % len(selected)] for i in range(4)]
                        before = len(upstream.calls)
                        jobs = [
                            asyncio.create_task(call(starters[i], f"{label}-{i:03}", mode="hold"))
                            for i in range(4)
                        ]

                        try:
                            await wait_for(four_active)
                            jobs += [
                                asyncio.create_task(
                                    call(
                                        selected[i % len(selected)],
                                        f"{label}-{i:03}",
                                        mode="hold",
                                        stream=bool(i % 2),
                                    )
                                )
                                for i in range(4, args.burst_clients)
                            ]

                            async def saturated():
                                h = await health()
                                return (
                                    h["capacity"]["active"] == 4
                                    and h["capacity"]["queued"] == 8
                                    and sum(task.done() for task in jobs) == args.burst_clients - 12
                                )

                            await wait_for(saturated)
                        finally:
                            upstream.hold.set()
                            results = await asyncio.gather(*jobs)
                        await wait_for(idle)
                        check(
                            label + "_bounded_execution_and_queue",
                            Counter(x["status"] for x in results)
                            == {200: 12, 429: args.burst_clients - 12}
                            and len(upstream.calls) - before == 12
                            and all(
                                x["error"] == "server_overloaded"
                                for x in results
                                if x["status"] == 429
                            ),
                            status_counts=dict(Counter(x["status"] for x in results)),
                            upstream_calls=12,
                        )

                    for cycle in range(args.burst_cycles):
                        await burst(f"mixed_burst_{cycle + 1}", names)
                    if args.four_api:
                        representatives = {}
                        for name, (connection, _) in apps.items():
                            representatives.setdefault(connection, name)
                        for connection in representatives:
                            await burst(
                                "single_" + connection,
                                [name for name in names if apps[name][0] == connection],
                            )

                        check(
                            "all_four_apis_overlap_and_each_reaches_four_execution_slots",
                            upstream.peak_distinct_connections == 4
                            and all(upstream.peak_by_connection[c] == 4 for c in representatives),
                            peak_distinct_connections=upstream.peak_distinct_connections,
                            peak_by_connection=dict(upstream.peak_by_connection),
                        )
                        upstream.hold.clear()
                        before = len(upstream.calls)
                        held = [
                            asyncio.create_task(call(name, "queue-holder", mode="hold"))
                            for name in representatives.values()
                        ]
                        limits = app.state.admission.limits
                        original_timeout = limits.queue_timeout_seconds
                        try:
                            await wait_for(four_active)
                            queued = asyncio.create_task(call("openai-chat", "cancel-queued"))

                            async def one_queued():
                                return (await health())["capacity"]["queued"] == 1

                            await wait_for(one_queued)
                            queued.cancel()
                            await asyncio.gather(queued, return_exceptions=True)

                            async def queue_empty():
                                return (await health())["capacity"]["queued"] == 0

                            await wait_for(queue_empty)
                            report["cancelled_client_requests"] = 1
                            limits.queue_timeout_seconds = 0.05
                            timed_out = await call("openai-chat", "queue-timeout")
                            check(
                                "queued_cancel_and_timeout_never_reach_upstream",
                                timed_out["error"] == "queue_timeout"
                                and len(upstream.calls) - before == 4,
                            )
                        finally:
                            limits.queue_timeout_seconds = original_timeout
                            upstream.hold.set()
                            await asyncio.gather(*held)
                        await wait_for(idle)

                        fault_matrix = (
                            ("rate", False, "upstream_rate_limit"),
                            ("auth", False, "upstream_auth_error"),
                            ("server-error", False, "upstream_error"),
                            ("timeout", False, "upstream_timeout"),
                            ("malformed", False, "upstream_protocol_error"),
                            ("truncated", True, "upstream_protocol_error"),
                            ("disconnect", True, None),
                            ("unknown", False, None),
                        )
                        peers = list(representatives.values())
                        for index, name in enumerate(peers):
                            for mode, streaming, expected in fault_matrix:
                                before = len(upstream.calls)
                                failed, healthy = await asyncio.gather(
                                    call(name, "matrix-" + mode, mode=mode, stream=streaming),
                                    call(peers[(index + 1) % len(peers)], "neighbor-" + mode),
                                )
                                await wait_for(idle)
                                check(
                                    apps[name][0] + "_" + mode + "_isolated",
                                    failed["error"] == expected
                                    and healthy["correct_response"]
                                    and healthy["done"]
                                    and len(upstream.calls) - before == 2
                                    and (mode != "unknown" or failed["usage"] is None)
                                    and (mode != "disconnect" or not failed["done"]),
                                )
                    for name, token in tokens.items():
                        runtime.store.set_policy(
                            token["id"], {"max_concurrent_requests": apps[name][1]}
                        )

                    # Responses can precede final settlement; measure this boundary honestly.
                    sequential = [
                        await call("batch-indexer", f"immediate-followup-{i}") for i in range(10)
                    ]
                    await wait_for(idle)
                    check(
                        "immediate_followups_measure_finalization_window",
                        all(
                            x["correct_response"]
                            or x["status"] == 429
                            and x["error"] == "caller_concurrency_limit"
                            for x in sequential
                        ),
                        status_counts=dict(Counter(x["status"] for x in sequential)),
                        note="Concurrency one includes final ledger persistence after delivery",
                    )

                    upstream.hold.clear()
                    held = asyncio.create_task(call("batch-indexer", "noisy-held", mode="hold"))

                    async def one_active():
                        return upstream.active == 1

                    try:
                        await wait_for(one_active)
                        noisy = await asyncio.gather(
                            *(call("batch-indexer", f"noisy-{i}") for i in range(10))
                        )
                        neighbors = await asyncio.gather(
                            *(
                                call(name, "beside-noisy")
                                for name in apps
                                if name != "batch-indexer"
                            )
                        )

                        async def only_held_remains():
                            return (await health())["capacity"]["admitted"] == 1

                        await wait_for(only_held_remains)
                        after_cleanup = await asyncio.gather(
                            *(
                                call(name, "after-noisy-rejections")
                                for name in apps
                                if name != "batch-indexer"
                            )
                        )
                    finally:
                        upstream.hold.set()
                        await held
                    check(
                        "noisy_app_capped_shared_ingress_recovers_for_neighbors",
                        all(x["error"] == "caller_concurrency_limit" for x in noisy)
                        and all(
                            x["correct_response"] or x["error"] == "server_overloaded"
                            for x in neighbors
                        )
                        and all(x["correct_response"] for x in after_cleanup),
                        rejected=10,
                        immediate_neighbor_status_counts=dict(
                            Counter(x["status"] for x in neighbors)
                        ),
                        other_apps_succeeded_after_cleanup=len(apps) - 1,
                    )
                    await wait_for(idle)

                    for name, kind in (("daily-report", "tokens"), ("notes-summary", "budget")):
                        token = tokens[name]
                        before = runtime.store.quota(token["id"])
                        bodies = [payload(name, f"quota-{kind}-{i}", "hold") for i in range(4)]
                        quotes = [
                            ChatRequest.model_validate(body).upstream_payload(body["model"])
                            for body in bodies
                        ]
                        reserve = max(input_estimate(body) for body in quotes)
                        policy = {"max_concurrent_requests": None}
                        if kind == "tokens":
                            policy["daily_token_limit"] = (
                                before["charged_or_reserved_tokens"] + reserve + 16
                            )
                        else:
                            price = prices[apps[name][0]]
                            policy.update(
                                daily_budget=str(
                                    Decimal(before["estimated_cost_or_reservation"]["CNY"])
                                    + Decimal(estimate_cost(price, reserve, 16))
                                ),
                                budget_currency="CNY",
                            )
                        runtime.store.set_policy(token["id"], policy)
                        upstream.hold.clear()
                        first = asyncio.create_task(call(name, f"quota-{kind}-0", mode="hold"))
                        try:
                            await wait_for(one_active)
                            rejected = await asyncio.gather(
                                *(call(name, f"quota-{kind}-{i}", mode="hold") for i in range(1, 4))
                            )
                            neighbor = await call("ide-helper", "beside-quota-" + kind)
                        finally:
                            upstream.hold.set()
                            settled = await first
                        await wait_for(idle)
                        expected_error = (
                            "caller_token_quota" if kind == "tokens" else "caller_budget_quota"
                        )
                        check(
                            "concurrent_" + kind + "_reservation_is_per_app",
                            settled["correct_response"]
                            and neighbor["correct_response"]
                            and all(x["error"] == expected_error for x in rejected),
                            admitted=1,
                            rejected=3,
                        )
                        # Keep a finite policy with headroom for later fault injection.
                        runtime.store.set_policy(
                            token["id"],
                            {"daily_token_limit": 100000}
                            if kind == "tokens"
                            else {"daily_budget": "10"},
                        )

                    start = len(upstream.calls)
                    denied = await call(
                        "chat-desktop",
                        "forbidden-route",
                        overrides={
                            "connection_id": "relay-test",
                            "model": resources["relay-test"][1],
                        },
                    )
                    spoof = await call(
                        "chat-desktop",
                        "spoof-caller",
                        overrides={"caller_id": tokens["ide-helper"]["id"]},
                    )
                    check(
                        "permissions_and_identity_are_not_client_claims",
                        denied["status"] == 403
                        and spoof["status"] == 400
                        and len(upstream.calls) == start,
                    )

                    faults = [
                        ("chat-desktop", "disconnect", True),
                        ("ide-helper", "rate", False),
                        ("notes-summary", "malformed", False),
                        ("translation-worker", "truncated", True),
                        ("batch-indexer", "timeout", False),
                        ("daily-report", "unknown", False),
                    ]
                    before_faults = len(upstream.calls)
                    results = await asyncio.gather(
                        *(
                            call(name, "fault-" + mode, mode=mode, stream=stream)
                            for name, mode, stream in faults
                        ),
                        call("chat-desktop", "healthy-during-faults"),
                    )
                    await wait_for(idle)
                    check(
                        "six_faults_do_not_retry_or_block_healthy_request",
                        results[-1]["correct_response"]
                        and results[1]["error"] == "upstream_rate_limit"
                        and results[1]["retry_after"] == "2"
                        and results[2]["error"] == "upstream_protocol_error"
                        and results[3]["error"] == "upstream_protocol_error"
                        and results[4]["error"] == "upstream_timeout"
                        and results[5]["done"]
                        and results[5]["usage"] is None
                        and len(upstream.calls) - before_faults == 7,
                        actual_simulated_attempts=7,
                    )
                    unknown_tokens = await call("daily-report", "after-unknown-tokens")
                    unknown_cost = await call("notes-summary", "after-unknown-cost")
                    check(
                        "unknown_holds_only_block_affected_limited_apps",
                        unknown_tokens["error"] == "quota_usage_unknown"
                        and unknown_cost["error"] == "budget_cost_unknown",
                    )
                    runtime.store.revoke(tokens["batch-indexer"]["id"])
                    revoked, alive = await asyncio.gather(
                        call("batch-indexer", "after-revocation"),
                        call("chat-desktop", "after-neighbor-revocation"),
                    )
                    check(
                        "revoking_one_application_keeps_others_usable",
                        revoked["status"] == 401 and alive["correct_response"],
                    )
                    await wait_for(idle)
                    rows = runtime.store.records({}, limit=10000)
                    by_id = {r["request_id"]: r for r in rows}
                    for outcome in outcomes:
                        row = by_id.get(outcome["request_id"])
                        if row is None:
                            assert outcome["error"] == "server_overloaded"
                            continue
                        assert (
                            row["caller_id"] == tokens[outcome["application"]]["id"]
                            or outcome["status"] == 401
                        )
                        if outcome["done"] and outcome["mode"] != "unknown":
                            assert row["total_tokens"] == 14 and row["usage_known"]
                        assert row["upstream_attempts"] in (0, 1)
                    check(
                        "every_response_attributed_once_and_usage_not_doubled",
                        sum(r["upstream_attempts"] for r in rows) == len(upstream.calls)
                        and len(by_id) == len(rows),
                    )
                    report["applications"] = {
                        name: {
                            "connection": connection,
                            "policy_phase_concurrency_limit": limit,
                            "http_requests": sum(x["application"] == name for x in outcomes),
                            "http_status_counts": dict(
                                Counter(x["status"] for x in outcomes if x["application"] == name)
                            ),
                            "summary": runtime.store.summary({"caller_id": tokens[name]["id"]})[
                                "summary"
                            ],
                        }
                        for name, (connection, limit) in apps.items()
                    }
                    before_restart = runtime.store.summary({}, group_by="caller")
                    report["final_health"] = await health()
                recovery = runtime.recover_records()
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                sock.listen(128)
                before = len(upstream.calls)
                async with serve(app_for(runtime, upstream), runtime, sock):
                    check(
                        "restart_preserves_each_application_and_unknown_reservations",
                        runtime.store.summary({}, group_by="caller") == before_restart
                        and len(upstream.calls) == before
                        and not any(recovery.values()),
                    )
                report.update(
                    passed=True,
                    application_count=len(apps),
                    client_requests=len(outcomes) + report.get("cancelled_client_requests", 0),
                    simulated_upstream_requests=len(upstream.calls),
                    peak_upstream_concurrency=upstream.peak,
                    peak_distinct_connections=upstream.peak_distinct_connections,
                    peak_by_connection=dict(upstream.peak_by_connection),
                    latency_ms=percentiles(
                        [x["duration_ms"] for x in outcomes if x["correct_response"] and x["done"]]
                    ),
                    error_counts=dict(Counter(x["error"] for x in outcomes if x["error"])),
                    recovery_after_stop=recovery,
                    temporary_service_stopped=True,
                )
            except Exception as exc:
                report.update(passed=False, failure_type=type(exc).__name__)
                raise
            finally:
                report["outcomes"] = outcomes
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report["temporary_profiles_removed"] = True
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"outcomes", "applications"}},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests-per-app", type=int, default=100, choices=range(2, 501))
    parser.add_argument("--four-api", action="store_true", help="Add two OpenAI applications")
    parser.add_argument("--burst-clients", type=int, default=48, choices=range(16, 257))
    parser.add_argument("--burst-cycles", type=int, default=1, choices=range(1, 11))
    asyncio.run(run(parser.parse_args()))
