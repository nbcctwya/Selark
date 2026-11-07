"""Measure an isolated gateway with fake upstreams; never load a user's profile.

Run each comparison in a fresh process with the same Python/environment. CPU and
RSS include the in-process synthetic caller, so these are comparison measurements,
not an estimate of every deployment's minimum memory. No listening sockets needed.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

from selark.app import create_app
from selark.cli import parser
from selark.config import initialize
from selark.runtime import Runtime


def rss_bytes():
    if os.name == "nt":
        import win32api
        import win32process

        return win32process.GetProcessMemoryInfo(win32api.GetCurrentProcess())["WorkingSetSize"]
    path = Path("/proc/self/status")
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return None


async def measure(runtime, *, idle_seconds, requests):
    calls, scans = 0, 0
    messages = [
        {"role": "system", "content": "Caller supplied system text.\n"},
        {"role": "user", "content": "A short synthetic request. 中文 😀"},
    ]
    usage = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}

    def upstream(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert payload["messages"] == messages
        assert "instructions" not in payload and "session_id" not in payload
        return httpx.Response(
            200,
            json={
                "id": "fake-response",
                "object": "chat.completion",
                "model": "fake-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )

    app = create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream))
    journal = app.state.storage.journal
    entries = journal.entries

    def count_scans():
        nonlocal scans
        scans += 1
        return entries()

    journal.entries = count_scans
    token = runtime.store.create_token("measurement")["token"]
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://selark"
        ) as client,
    ):
        # Warm up framework, provider routing, database and worker pools.
        async def post():
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + token},
                json={"messages": messages, "max_tokens": 16, "session_id": "same-session"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["usage"] == usage

        await post()
        await asyncio.sleep(1.1)
        start_scans, start_calls = scans, calls
        start_cpu, start = time.process_time(), time.perf_counter()
        await asyncio.sleep(idle_seconds)
        idle_wall, idle_cpu = time.perf_counter() - start, time.process_time() - start_cpu
        idle_scans = scans - start_scans
        assert calls == start_calls
        idle_rss = rss_bytes()

        start_cpu, start = time.process_time(), time.perf_counter()
        for _ in range(requests):
            await post()
        active_wall, active_cpu = time.perf_counter() - start, time.process_time() - start_cpu
        assert calls == requests + 1
        assert runtime.store.summary({})["summary"]["known_tokens"]["total_tokens"] == calls * 14
        return {
            "python": sys.version.split()[0],
            "source": str(Path(sys.modules["selark"].__file__).resolve()),
            "real_provider_requests": 0,
            "fake_generation_requests": calls,
            "forwarded_messages_unchanged": True,
            "reported_tokens": calls * 14,
            "idle_seconds": round(idle_wall, 3),
            "idle_cpu_seconds": round(idle_cpu, 6),
            "idle_journal_scans": idle_scans,
            "idle_rss_bytes": idle_rss,
            "requests": requests,
            "request_wall_seconds": round(active_wall, 6),
            "request_cpu_seconds": round(active_cpu, 6),
            "after_requests_rss_bytes": rss_bytes(),
            "jwt_loaded": "jwt" in sys.modules,
        }


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--idle-seconds", type=float, default=10)
    arguments.add_argument("--requests", type=int, default=100)
    arguments.add_argument("--output", type=Path)
    arguments.add_argument(
        "--check-invariants", action="store_true", help="Reject idle scans or eager JWT loading."
    )
    args = arguments.parse_args()
    if not 0 < args.idle_seconds <= 60 or not 1 <= args.requests <= 10000:
        arguments.error("idle-seconds must be in (0, 60], requests in [1, 10000]")
    if args.check_invariants and args.idle_seconds < 2:
        arguments.error("checking idle behaviour requires at least 2 seconds")
    # Include command parsing imports, as in an ordinary selark serve process.
    parser()
    for name in list(os.environ):
        if name.startswith("SELARK_"):
            del os.environ[name]
    with tempfile.TemporaryDirectory(prefix="selark-measure-") as temporary:
        profile = Path(temporary) / "profile"
        initialize(profile, "fake-key", models=["fake-model"], default_model="fake-model")
        with Runtime(profile, data_path=profile / "data") as runtime, runtime.service():
            result = asyncio.run(
                measure(runtime, idle_seconds=args.idle_seconds, requests=args.requests)
            )
    document = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(document + "\n")
    print(document)
    if args.check_invariants:
        assert result["idle_journal_scans"] == 0, "Healthy idle recovery polled the filesystem."
        assert not result["jwt_loaded"], "Ordinary API relay loaded OAuth crypto dependencies."


if __name__ == "__main__":
    main()
