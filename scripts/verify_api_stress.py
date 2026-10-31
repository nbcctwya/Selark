"""Offline API stress acceptance using real local sockets and temporary fake credentials.

No user configuration or external provider is accessed. Runs an installed Selark process.
"""

import argparse
import asyncio
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from selark import __version__
from selark.config import initialize
from selark.persistence import Journal
from selark.runtime import Runtime
from selark.storage import Store

USAGE = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}


def event(final=False):
    return (
        "data: "
        + json.dumps(
            {
                "id": "fake-stress",
                "object": "chat.completion.chunk",
                "model": "shared",
                "choices": [
                    {
                        "index": 0,
                        "delta": {} if final else {"content": "x"},
                        "finish_reason": "stop" if final else None,
                    }
                ],
                **({"usage": USAGE} if final else {}),
            }
        )
        + "\n\n"
    ).encode()


class Upstream:
    def __init__(self):
        self.calls = []
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                assert self.path == "/v1/chat/completions"
                assert (
                    self.headers.get("api-key") == "fake-stress-mimo"
                    or self.headers.get("Authorization") == "Bearer fake-stress-relay"
                )
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert "session_id" not in data and "connection_id" not in data
                mode = data["messages"][-1]["content"]
                with owner.lock:
                    owner.calls.append(mode)
                    owner.active += 1
                    owner.peak = max(owner.peak, owner.active)
                try:
                    if mode == "hold":
                        assert owner.release.wait(8)
                    if data.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        for _ in range(40 if mode == "long" else 2):
                            self.wfile.write(event())
                            self.wfile.flush()
                            time.sleep(0.025)
                        self.wfile.write(event(True) + b"data: [DONE]\n\n")
                        self.wfile.flush()
                    else:
                        body = json.dumps(
                            {
                                "id": "fake-stress",
                                "object": "chat.completion",
                                "model": "shared",
                                "choices": [
                                    {
                                        "index": 0,
                                        "message": {"role": "assistant", "content": "OK"},
                                        "finish_reason": "stop",
                                    }
                                ],
                                "usage": USAGE,
                            }
                        ).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with owner.lock:
                        owner.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


async def until(check, timeout=8):
    async with asyncio.timeout(timeout):
        while not (value := await check()):
            await asyncio.sleep(0.01)
        return value


async def exercise(client, runtime, upstream, callers):
    checks = []
    client_attempts = 0

    async def health():
        response = await client.get("/health")
        assert response.status_code == 200
        return response.json()

    async def idle():
        h = await health()
        return h["capacity"]["admitted"] == 0 and h["storage"]["pending_records"] == 0

    def auth(who=0):
        return {"Authorization": "Bearer " + callers[who]["token"]}

    async def post(mode="fast", session="after", who=0, connection="mimo-default", stream=False):
        nonlocal client_attempts
        client_attempts += 1
        return await client.post(
            "/v1/chat/completions",
            headers=auth(who),
            json={
                "messages": [{"role": "user", "content": mode}],
                "max_tokens": 8,
                "stream": stream,
                "session_id": session,
                "connection_id": connection,
            },
        )

    for cycle in range(3):
        upstream.release.clear()
        start_calls = len(upstream.calls)
        tasks = [
            asyncio.create_task(
                post(
                    "hold",
                    f"burst-{cycle}",
                    who=i % 2,
                    connection="relay-test" if i % 3 else "mimo-default",
                    stream=bool(i % 2),
                )
            )
            for i in range(80)
        ]

        async def full(tasks=tasks):
            h = await health()
            return (
                h["capacity"]["active"] == 4
                and h["capacity"]["queued"] == 8
                and sum(task.done() for task in tasks) == 68
            )

        try:
            await until(full)
            latency = time.monotonic()
            await health()
            latency = (time.monotonic() - latency) * 1000
        finally:
            upstream.release.set()
        responses = await asyncio.gather(*tasks)
        await until(idle)
        counts = Counter(response.status_code for response in responses)
        assert counts == {200: 12, 429: 68}, counts
        assert len(upstream.calls) - start_calls == 12 and upstream.peak <= 4
        by_id = {
            r["request_id"]: r for r in runtime.store.records({"session_id": f"burst-{cycle}"})
        }
        for i, response in enumerate(responses):
            if response.status_code == 429:
                assert response.json()["error"]["code"] == "server_overloaded"
                assert response.headers["retry-after"] == "1"
            else:
                row = by_id[response.headers["x-request-id"]]
                assert row["caller_id"] == callers[i % 2]["id"]
                assert row["connection_id"] == ("relay-test" if i % 3 else "mimo-default")
                assert row["total_tokens"] == 14 and row["upstream_attempts"] == 1
        checks.append(
            {
                "check": f"80_simultaneous_clients_cycle_{cycle + 1}",
                "passed": True,
                "status_counts": dict(counts),
                "upstream_calls": 12,
                "health_latency_ms": round(latency, 2),
            }
        )

    upstream.release.clear()
    before = len(upstream.calls)
    cancelled = [asyncio.create_task(post("hold", "cancel-all")) for _ in range(12)]

    async def all_queued():
        return (await health())["capacity"]["queued"] == 8 and len(upstream.calls) == before + 4

    await until(all_queued)
    for task in cancelled:
        task.cancel()
    await asyncio.gather(*cancelled, return_exceptions=True)
    await until(idle)
    upstream.release.set()
    assert len(upstream.calls) == before + 4
    rows = runtime.store.records({"session_id": "cancel-all"})
    assert len(rows) == 12 and all(r["status"] == "cancelled" for r in rows)
    assert sum(r["upstream_attempts"] for r in rows) == 4
    assert all(r["total_tokens"] is None for r in rows)
    checks.append(
        {"check": "12_simultaneous_disconnects_release_active_and_queued", "passed": True}
    )
    await asyncio.sleep(0.05)

    upstream.release.clear()
    active = [asyncio.create_task(post("hold", "management-inflight")) for _ in range(4)]

    async def active_four():
        return (await health())["capacity"]["active"] == 4

    await until(active_four)
    queued = [
        asyncio.create_task(post("fast", "revoked-inflight", who=1)),
        asyncio.create_task(post("fast", "disabled-inflight", connection="relay-test")),
    ]

    async def queued_two():
        return (await health())["capacity"]["queued"] == 2

    await until(queued_two)
    await asyncio.to_thread(runtime.store.revoke, callers[1]["id"])
    await asyncio.to_thread(runtime.change_connection, {"enabled": False}, "relay-test")
    assert (await post(who=1, session="revoked-new")).status_code == 401
    upstream.release.set()
    assert all(r.status_code == 200 for r in await asyncio.gather(*active))
    old, disabled = await asyncio.gather(*queued)
    assert old.status_code == 401 and old.json()["error"]["code"] == "invalid_token"
    assert disabled.status_code == 503 and disabled.json()["error"]["code"] == "connection_disabled"
    await until(idle)
    assert runtime.store.records({"session_id": "disabled-inflight"})[0]["upstream_attempts"] == 0
    assert runtime.store.records({"session_id": "revoked-inflight"})[0]["upstream_attempts"] == 0
    checks.append(
        {
            "check": "queued_revocation_and_connection_disable_follow_documented_rules",
            "passed": True,
        }
    )

    before = len(upstream.calls)
    bad_cases = [
        ({"messages": []}, 400),
        ({"messages": [{"role": "user", "content": "x"}] * 257}, 400),
        ({"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}, 400),
        ({"messages": [{"role": "user", "content": "x"}], "caller_id": callers[1]["id"]}, 400),
        ({"messages": [{"role": "user", "content": "x"}], "connection_id": "missing"}, 400),
        ({"messages": [{"role": "user", "content": "x"}], "model": "not-allowed"}, 400),
        ({"messages": [{"role": "user", "content": "x"}], "stream": "true"}, 400),
    ]
    for payload, code in bad_cases:
        assert (
            await client.post("/v1/chat/completions", headers=auth(), json=payload)
        ).status_code == code
    assert (
        await client.post("/v1/chat/completions", headers=auth(), content=b"x" * (1024 * 1024 + 1))
    ).status_code == 413
    assert (
        await client.post(
            "/v1/chat/completions", headers=[*auth().items(), *auth().items()], json={}
        )
    ).status_code == 401

    async def slow_body():
        yield b'{"messages":'
        await asyncio.sleep(0.4)
        yield b"[]}"

    assert (
        await client.post(
            "/v1/chat/completions",
            headers=auth() | {"Content-Type": "application/json"},
            content=slow_body(),
        )
    ).status_code == 408
    client_attempts += len(bad_cases) + 3
    await until(idle)
    assert len(upstream.calls) == before
    checks.append({"check": "10_invalid_or_slow_requests_generate_nothing", "passed": True})

    before_tokens = runtime.store.summary({})["summary"]["known_tokens"]["total_tokens"]
    chunk_counts = [0] * 4

    async def consume(index):
        nonlocal client_attempts
        client_attempts += 1
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth(),
            json={
                "messages": [{"role": "user", "content": "long"}],
                "stream": True,
                "session_id": "locked-streams",
                "max_tokens": 8,
            },
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    chunk_counts[index] += 1

    streams = [asyncio.create_task(consume(i)) for i in range(4)]

    async def streaming():
        return min(chunk_counts) >= 2

    await until(streaming)
    lock = sqlite3.connect(runtime.store.path)
    lock.execute("BEGIN IMMEDIATE")
    try:
        before_chunks = sum(chunk_counts)
        before_time = time.monotonic()
        await health()
        health_ms = (time.monotonic() - before_time) * 1000
        await asyncio.sleep(0.2)
        assert sum(chunk_counts) > before_chunks + 8
        await asyncio.gather(*streams)
        assert all(n == 42 for n in chunk_counts)
        blocked = await post(session="writer-blocked")
        assert blocked.status_code == 503
        assert blocked.json()["error"]["code"] == "local_state_unavailable"
        pending = (await health())["storage"]["pending_records"]
        assert pending >= 4
        backup = await asyncio.to_thread(runtime.backup, "stress-while-writer-locked")
        with contextlib.closing(Store(backup / "records.sqlite3")) as restored:
            journal = Journal(backup / "pending")
            replay = journal.replay(restored, include_unsettled=True)
            assert replay["failed"] == replay["invalid"] == 0
            tokens = restored.summary({})["summary"]["known_tokens"]["total_tokens"]
            assert tokens == before_tokens + 56
            assert journal.replay(restored, include_unsettled=True)["restored"] == 0
    finally:
        lock.rollback()
        lock.close()
    await until(idle)
    assert runtime.store.summary({})["summary"]["known_tokens"]["total_tokens"] == tokens
    checks.append(
        {
            "check": "4_live_streams_with_sqlite_writer_and_consistent_pending_backup",
            "passed": True,
            "data_events_per_stream": chunk_counts,
            "pending_at_backup": pending,
            "health_latency_ms": round(health_ms, 2),
            "recovered_tokens": 56,
        }
    )
    assert (await post(session="after-all-faults")).status_code == 200
    await until(idle)
    records = runtime.store.records({}, limit=1000)
    summary = runtime.store.summary({})["summary"]
    assert sum(r["upstream_attempts"] or 0 for r in records) == len(upstream.calls)
    assert not any(r["status"] == "running" for r in records)
    assert all(r["total_tokens"] is None for r in records if r["status"] != "success")
    assert upstream.peak <= 4
    return {
        "version": __version__,
        "verified_at": datetime.now(UTC).isoformat(),
        "scope": "temporary loopback upstream only; no real credentials or generations",
        "real_generation_requests": 0,
        "client_chat_requests": client_attempts,
        "simulated_upstream_requests": len(upstream.calls),
        "peak_upstream_concurrency": upstream.peak,
        "checks": checks,
        "summary": summary,
        "final_health": await health(),
    }


async def run(args):
    upstream = Upstream()
    service = None
    try:
        with tempfile.TemporaryDirectory(prefix="selark-api-stress-") as tmp:
            directory = Path(tmp) / "profile"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            base = f"http://127.0.0.1:{upstream.server.server_port}/v1"
            initialize(
                directory,
                "fake-stress-mimo",
                base_url=base,
                models=["shared"],
                default_model="shared",
                port=port,
            )
            with Runtime(directory) as runtime:
                runtime.change_limits(
                    {
                        "max_concurrent_requests": 4,
                        "max_queued_requests": 8,
                        "queue_timeout_seconds": 5.0,
                        "body_timeout_seconds": 0.2,
                        "request_timeout_seconds": 8.0,
                    }
                )
                runtime.add_connection(
                    "relay-test",
                    "openai-compatible",
                    "fake-stress-relay",
                    base_url=base,
                    allowed_models=["shared"],
                )
                callers = [runtime.store.create_token("A"), runtime.store.create_token("B")]
                env = {k: v for k, v in os.environ.items() if not k.startswith("SELARK_")}
                with open(Path(tmp) / "service.log", "w") as log:
                    service = subprocess.Popen(
                        [args.selark, "--config-dir", str(directory), "serve"],
                        cwd=tmp,
                        env=env,
                        stdout=log,
                        stderr=log,
                    )
                    async with httpx.AsyncClient(
                        base_url=url,
                        trust_env=False,
                        timeout=12,
                        limits=httpx.Limits(max_connections=100),
                    ) as client:

                        async def ready():
                            assert service.poll() is None, "Temporary service exited"
                            try:
                                return (await client.get("/health")).status_code == 200
                            except httpx.RequestError:
                                return False

                        await until(ready)
                        result = await exercise(client, runtime, upstream, callers)
                    service.terminate()
                    service.wait(timeout=10)
                    service = None
                result["recovery_after_stop"] = runtime.recover_records()
                assert not any(result["recovery_after_stop"].values())
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        upstream.release.set()
        if service:
            service.terminate()
            service.wait(timeout=10)
        upstream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selark", required=True)
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(run(parser.parse_args()))
