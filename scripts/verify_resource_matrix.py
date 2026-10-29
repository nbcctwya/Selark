"""Four-resource offline acceptance: real loopback HTTP/CLI, fake upstream transport only.

Uses temporary profiles and invented keys; no real profile or provider network is accessed.
Run with the desired Selark Python (venv or pipx); --selark optionally selects a CLI executable.
"""

import argparse
import asyncio
import contextlib
import json
import os
import pty
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn

from selark import __version__
from selark.config import _write_private, private_directory
from selark.persistence import Journal
from selark.storage import Store, utc_now

ROOT = Path(__file__).resolve().parents[1]
CLIENT_KEEPALIVE_SECONDS = 0.5  # Retire idle sockets before the fixture's 2 s timeout.


async def wait_for(check):
    async with asyncio.timeout(8):
        while not (value := await check()):
            await asyncio.sleep(0.01)
        return value


@asynccontextmanager
async def serve(app, runtime, sock):
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            timeout_graceful_shutdown=2,
            timeout_keep_alive=2,
        )
    )
    with runtime.service():
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:

            async def ready():
                assert not task.done(), "Temporary service exited"
                return server.started

            await wait_for(ready)
            yield server
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


async def mixed_burst(cycle, url, post, health, idle, upstream, gate, output):
    """One pool per burst; preserve the first failure and settle every submitted task."""
    from tests.four_resources import PLAN, RESOURCES

    identities = list(RESOURCES)
    upstream.hold.clear()
    start = len(upstream.calls)
    jobs, results = [], []
    primary = None
    phase = "four_active"
    async with httpx.AsyncClient(
        base_url=url,
        trust_env=False,
        timeout=10,
        limits=httpx.Limits(max_connections=100, keepalive_expiry=CLIENT_KEEPALIVE_SECONDS),
    ) as burst:

        def submit(i):
            return asyncio.create_task(
                post(
                    identities[i % 4],
                    mode="hold",
                    stream=bool(i % 2),
                    who=i % 2,
                    session=f"burst-{cycle}",
                    http=burst,
                )
            )

        async def four_active():
            return all(upstream.active[k] == 1 for k in identities)

        async def settled_queue():
            h = await health()
            return (
                sum(t.done() for t in jobs) + h["capacity"]["active"] + h["capacity"]["queued"]
                == 64
            )

        try:
            jobs = [submit(i) for i in range(4)]
            await wait_for(four_active)
            phase = "settled_queue"
            jobs += [submit(i) for i in range(4, 64)]
            await wait_for(settled_queue)
            phase = "health_at_capacity"
            start_health = time.monotonic()
            h = await health()
            latency = round((time.monotonic() - start_health) * 1000, 2)
            assert h["capacity"]["active"] == 4 and h["capacity"]["queued"] <= 8
        except Exception as exc:
            primary = exc
        finally:
            before_release = gate.health()
            upstream.hold.set()
            # A second exception here used to hide the phase that first failed,
            # and leave sibling requests running during service shutdown.
            results = await asyncio.gather(*jobs, return_exceptions=True)
        errors = [
            {"client": i, "exception": type(r).__name__}
            for i, r in enumerate(results)
            if isinstance(r, BaseException)
        ]
        counts = Counter(r.status_code for r in results if isinstance(r, httpx.Response))
        try:
            if primary:
                raise primary
            assert not errors, f"Client transport failures: {errors}"
            phase = "post_burst_accounting"
            await wait_for(idle)
            assert set(counts) == {200, 429} and 4 <= counts[200] <= 12, counts
            assert len(upstream.calls) - start == counts[200]
            assert upstream.peak[PLAN] == 1 and upstream.peak_total == 4
        except Exception as exc:
            diagnostic = {
                "cycle": cycle + 1,
                "phase": phase,
                "exception": type(exc).__name__,
                "clients_submitted": len(jobs),
                "clients_settled": len(results),
                "client_errors": errors,
                "status_counts": dict(counts),
                "before_release": before_release,
                "after_release": gate.health(),
                "upstream_calls": len(upstream.calls) - start,
                "real_generation_requests": 0,
            }
            failure = output.with_suffix(".failure.json")
            failure.write_text(json.dumps(diagnostic, indent=2) + "\n")
            print(json.dumps(diagnostic), file=sys.stderr)
            raise
    return {
        "check": f"64_mixed_clients_cycle_{cycle + 1}",
        "passed": True,
        "status_counts": dict(counts),
        "upstream_calls": counts[200],
        "peak_active": 4,
        "peak_plan_active": 1,
        "health_latency_ms": latency,
        "client_transport_errors": 0,
        "generation_retries": 0,
    }


async def run(args):
    # Fixtures are deliberately test-only; the installed Selark runtime remains the one
    # imported by this interpreter. Never change official Coding Plan URL restrictions.
    sys.path.insert(0, str(ROOT))
    from tests.four_resources import (
        PLAN,
        RESOURCES,
        FourUpstreams,
        app_for,
        auth,
        caller,
        configure,
        payload,
    )

    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SELARK_")
        and k not in {"VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH"}
        and not k.endswith(("_API_KEY", "_TOKEN"))
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        tempfile.TemporaryDirectory(prefix="selark-resource-simulation-") as tmp,
    ):
        root = Path(tmp)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(128)
        url = f"http://127.0.0.1:{port}"
        with configure(root / "config", port=port) as runtime:
            upstream = FourUpstreams()
            tokens = [caller(runtime, "A"), caller(runtime, "B")]
            checks, ids = [], list(RESOURCES)
            http_requests = 0
            cli_requests = 0
            command = [str(args.selark)] if args.selark else [sys.executable, "-m", "selark"]
            app = app_for(
                runtime,
                upstream,
                max_concurrent_requests=4,
                max_queued_requests=8,
                queue_timeout_seconds=5.0,
                request_timeout_seconds=8.0,
            )
            async with (
                serve(app, runtime, sock),
                httpx.AsyncClient(
                    base_url=url,
                    trust_env=False,
                    timeout=10,
                    limits=httpx.Limits(
                        max_connections=100, keepalive_expiry=CLIENT_KEEPALIVE_SECONDS
                    ),
                ) as client,
            ):

                async def health():
                    r = await client.get("/health")
                    assert r.status_code == 200
                    return r.json()

                async def idle():
                    h = await health()
                    return h["capacity"]["admitted"] == 0 and h["storage"]["pending_records"] == 0

                async def post(identity, *, who=0, session="matrix", http=None, **options):
                    nonlocal http_requests
                    http_requests += 1
                    return await (http or client).post(
                        "/v1/chat/completions",
                        headers=auth(tokens[who]),
                        json=payload(identity, **options) | {"session_id": session},
                    )

                for identity in ids:
                    for stream in (False, True):
                        r = await post(
                            identity,
                            stream=stream,
                            history=True,
                            blocks=True,
                            route="alias" if stream else "qualified",
                            who=int(stream),
                        )
                        assert r.status_code == 200
                        assert (
                            "[DONE]" in r.text
                            if stream
                            else r.json()["usage"]["total_tokens"] == 14
                        )
                await wait_for(idle)
                grouped = runtime.store.sessions({"session_id": "matrix"})["groups"]
                assert len(grouped) == 2 and all(
                    g["known_tokens"]["total_tokens"] == 56 for g in grouped
                )
                checks.append(
                    {
                        "check": "four_resources_http_multiturn_text_blocks_routes_sessions",
                        "passed": True,
                        "requests": 8,
                    }
                )

                profile = root / "client"
                private_directory(profile)
                _write_private(
                    profile / "client.json",
                    {
                        "url": url,
                        "access_token": tokens[0]["token"],
                        "model": RESOURCES["mimo-default"][1],
                    },
                )

                async def cli(arguments, *, stdin=None, terminal=False, executable=None):
                    argv = executable or [*command, "--config-dir", str(profile)]
                    descriptors = pty.openpty() if terminal else None
                    try:
                        kwargs = {"stdin": descriptors[1]} if terminal else {"input": stdin}
                        result = await asyncio.to_thread(
                            subprocess.run,
                            [*argv, *arguments],
                            cwd=tmp,
                            env=environment,
                            capture_output=True,
                            text=True,
                            timeout=10,
                            **kwargs,
                        )
                    finally:
                        if descriptors:
                            for fd in descriptors:
                                os.close(fd)
                    return result

                for identity in ids:
                    messages = root / (identity + ".json")
                    _write_private(messages, payload(identity, history=True)["messages"])
                    cases = [
                        (
                            [
                                "ask",
                                "fast",
                                "--system",
                                "Check Python",
                                "--connection",
                                identity,
                                "--json",
                            ],
                            None,
                        ),
                        (
                            ["ask", "--alias", identity, "--messages", str(messages), "--stream"],
                            None,
                        ),
                    ]
                    if identity != PLAN:
                        cases += [
                            (
                                [
                                    "ask",
                                    "-",
                                    "--model",
                                    identity + "::" + RESOURCES[identity][1],
                                    "--json",
                                ],
                                "fast",
                            )
                        ]
                    for arguments, text in cases:
                        arguments += ["--max-tokens", "32"]
                        if identity == PLAN:
                            arguments += ["--interactive-coding"]
                        result = await cli(arguments, stdin=text, terminal=identity == PLAN)
                        assert result.returncode == 0, result.stderr
                        assert "你好 🐍" in result.stdout
                        cli_requests += 1
                before = len(upstream.calls)
                refused = await cli(["ask", "fast", "--connection", PLAN, "--interactive-coding"])
                assert refused.returncode == 1 and "交互终端" in refused.stderr
                assert len(upstream.calls) == before
                for identity in ids[:-1]:
                    external = root / (identity + "-client.json")
                    _write_private(
                        external,
                        {
                            "url": url,
                            "access_token": tokens[1]["token"],
                            "model": identity + "::" + RESOURCES[identity][1],
                        },
                    )
                    result = await cli(
                        ["--config", str(external)],
                        stdin="A short article.",
                        executable=[sys.executable, str(ROOT / "examples/summarize.py")],
                    )
                    assert result.returncode == 0 and "你好 🐍" in result.stdout, result.stderr
                    cli_requests += 1
                await wait_for(idle)
                checks.append(
                    {
                        "check": "cli_inputs_routes_json_stream_and_external_program",
                        "passed": True,
                        "successful_generations": cli_requests,
                        "noninteractive_plan_cli_rejected": True,
                    }
                )

                for cycle in range(args.cycles):
                    checks.append(
                        await mixed_burst(
                            cycle,
                            url,
                            post,
                            health,
                            idle,
                            upstream,
                            app.state.admission,
                            args.output,
                        )
                    )

                # One caller's pending token reservation is shared across all four resources.
                limited = caller(runtime, "limited", daily_token_limit=600)
                upstream.hold.clear()

                async def quota_call(identity):
                    nonlocal http_requests
                    http_requests += 1
                    return await client.post(
                        "/v1/chat/completions",
                        headers=auth(limited),
                        json=payload(identity, mode="hold"),
                    )

                jobs = [asyncio.create_task(quota_call(identity)) for identity in ids]
                try:

                    async def limited_ready():
                        return (
                            sum(upstream.active.values()) == 1 and sum(t.done() for t in jobs) == 3
                        )

                    await wait_for(limited_ready)
                finally:
                    upstream.hold.set()
                    results = await asyncio.gather(*jobs, return_exceptions=True)
                assert Counter(r.status_code for r in results) == {200: 1, 429: 3}
                await wait_for(idle)
                assert runtime.store.quota(limited["id"])["charged_or_reserved_tokens"] == 14
                checks.append(
                    {
                        "check": "shared_caller_token_reservation_across_four_resources",
                        "passed": True,
                        "successful_generations": 1,
                        "quota_rejections": 3,
                        "settled_tokens": 14,
                    }
                )

                start = len(upstream.calls)

                async def disconnect(identity):
                    nonlocal http_requests
                    http_requests += 1
                    async with client.stream(
                        "POST",
                        "/v1/chat/completions",
                        headers=auth(tokens[0]),
                        json=payload(identity, mode="cancel", stream=True)
                        | {"session_id": "disconnect"},
                    ) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                break

                await asyncio.gather(*[disconnect(identity) for identity in ids])
                await wait_for(idle)
                cancelled = runtime.store.records({"session_id": "disconnect"})
                assert len(cancelled) == 4 and len(upstream.calls) - start == 4
                assert all(
                    r["total_tokens"] is None and r["status"] in {"cancelled", "failed"}
                    for r in cancelled
                )
                assert not any(upstream.active.values()) and not app.state.admission.plans
                checks.append(
                    {
                        "check": "four_resources_socket_disconnect_release",
                        "passed": True,
                        "unknown_usage_requests": 4,
                    }
                )

                start = len(upstream.calls)
                jobs = [
                    asyncio.create_task(
                        post(identity, mode="long", stream=True, session="database-contention")
                    )
                    for identity in ids
                ]

                async def streams_ready():
                    return len(upstream.calls) - start == 4

                await wait_for(streams_ready)
                writer = sqlite3.connect(runtime.store.path)
                writer.execute("BEGIN IMMEDIATE")
                try:
                    results = await asyncio.gather(*jobs)
                    assert all(r.status_code == 200 and "[DONE]" in r.text for r in results)

                    async def receipts_ready():
                        h = await health()
                        return (
                            h["capacity"]["admitted"] == 0
                            and len(list((runtime.data_dir / "pending").glob("*.json"))) == 4
                        )

                    await wait_for(receipts_ready)
                    backup = await asyncio.to_thread(runtime.backup, "four-resource-contention")
                finally:
                    writer.rollback()
                    writer.close()
                await wait_for(idle)
                with contextlib.closing(Store(backup / "records.sqlite3")) as restored:
                    journal = Journal(backup / "pending")
                    report = journal.replay(restored, include_unsettled=True)
                    assert report["restored"] == 4
                    assert (
                        restored.summary({"session_id": "database-contention"})["summary"][
                            "known_tokens"
                        ]["total_tokens"]
                        == 56
                    )
                    assert journal.replay(restored, include_unsettled=True)["restored"] == 0
                checks.append(
                    {
                        "check": "four_resource_streams_database_lock_backup_recovery",
                        "passed": True,
                        "restored_receipts": 4,
                        "known_total_tokens": 56,
                        "second_replay": 0,
                    }
                )

                for identity in ids:
                    response = await post(identity, mode="zero", session="zero")
                    assert (
                        response.status_code == 200
                        and response.json()["usage"]["total_tokens"] == 0
                    )
                    response = await post(identity, mode="unknown", session="unknown")
                    assert response.status_code == 200 and "usage" not in response.json()
                await wait_for(idle)
                for session, expected in (("zero", 0), ("unknown", None)):
                    rows = runtime.store.records({"session_id": session})
                    assert len(rows) == 4 and all(r["total_tokens"] == expected for r in rows)
                checks.append({"check": "four_resources_zero_vs_missing_usage", "passed": True})
                summary = runtime.store.summary({}, group_by="connection")
                assert summary["summary"]["upstream_attempts"] == len(upstream.calls)
                final_health = await health()
                assert final_health["storage"]["pending_records"] == 0
                assert not app.state.admission.plans
            # Restart from the same isolated configuration and ledger, with no generation.
            before = len(upstream.calls)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            app = app_for(runtime, upstream)
            async with (
                serve(app, runtime, sock),
                httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False
                ) as client,
            ):
                assert (await client.get("/health")).status_code == 200
                assert runtime.store.summary({}, group_by="connection") == summary
                assert len(upstream.calls) == before
            result = {
                "version": __version__,
                "verified_at": utc_now(),
                "scope": "temporary profiles; real loopback downstream; MockTransport upstream",
                "real_generation_requests": 0,
                "real_provider_metadata_requests": 0,
                "http_chat_requests": http_requests,
                "cli_and_example_generations": cli_requests,
                "simulated_upstream_generations": len(upstream.calls),
                "simulated_quota_reads": len(upstream.metadata),
                "per_connection_generations": dict(
                    Counter(c["connection_id"] for c in upstream.calls)
                ),
                "peak_upstream_concurrency": upstream.peak_total,
                "peak_per_connection": dict(upstream.peak),
                "checks": checks,
                "summary": summary,
                "final_health": final_health,
                "restart_preserved_summary": True,
                "temporary_service_stopped": True,
            }
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps(
                    {k: v for k, v in result.items() if k not in {"summary", "final_health"}},
                    ensure_ascii=False,
                    indent=2,
                )
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selark", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=3, choices=range(1, 51))
    asyncio.run(run(parser.parse_args()))
