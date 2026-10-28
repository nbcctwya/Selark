"""Offline socket/process acceptance. No real profile or external provider is accessed.

Run with a Python containing Selark, optionally --selark /absolute/path/to/selark.
Every upstream request goes to an ephemeral loopback HTTP fixture with invented credentials.
"""

import argparse
import concurrent.futures
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from selark import __version__
from selark.config import initialize

USAGE = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}


def completion():
    return {
        "id": "fake",
        "object": "chat.completion",
        "model": "mimo-test",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
        ],
        "usage": USAGE,
    }


def chunk(*, final=False):
    return (
        "data: "
        + json.dumps(
            {
                "id": "fake",
                "object": "chat.completion.chunk",
                "model": "mimo-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {} if final else {"content": "OK"},
                        "finish_reason": "stop" if final else None,
                    }
                ],
                **({"usage": USAGE} if final else {}),
            }
        )
        + "\n\n"
    )


class Fixture:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                assert self.path == "/v1/chat/completions"
                assert self.headers["api-key"] == "test-stability-upstream-key"
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                prompt = data["messages"][-1]["content"]
                assert "session_id" not in data
                with owner.lock:
                    owner.calls.append(prompt)
                try:
                    if prompt == "hold":
                        owner.entered.set()
                        owner.release.wait(5)
                    if prompt in {"cancel", "shutdown"}:
                        owner.entered.set()
                        time.sleep(3)
                    if data.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        self.wfile.write(chunk().encode())
                        self.wfile.flush()
                        if prompt == "idle":
                            time.sleep(2)
                        if prompt == "crash":
                            owner.entered.set()
                            time.sleep(3)
                        if prompt == "trickle":
                            for _ in range(30):
                                time.sleep(0.1)
                                self.wfile.write(chunk().encode())
                                self.wfile.flush()
                        self.wfile.write((chunk(final=True) + "data: [DONE]\n\n").encode())
                        self.wfile.flush()
                    else:
                        body = json.dumps(completion()).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Local verification condition timed out")


def run(command):
    checks = []
    fixture = Fixture()
    service = logfile = None
    with tempfile.TemporaryDirectory(prefix="selark-stability-") as tmp:
        directory = Path(tmp) / "profile"
        outside = Path(tmp) / "outside-repository"
        outside.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        initialize(
            directory,
            "test-stability-upstream-key",
            base_url=f"http://127.0.0.1:{fixture.server.server_port}/v1",
            models=["mimo-test"],
            default_model="mimo-test",
            port=port,
        )
        settings = json.loads((directory / "server.json").read_text())
        settings["timeout_seconds"] = 1.0
        settings["limits"].update(
            max_concurrent_requests=1,
            max_queued_requests=1,
            queue_timeout_seconds=0.4,
            request_timeout_seconds=1.5,
            body_timeout_seconds=0.2,
            shutdown_grace_seconds=0.2,
        )
        (directory / "server.json").write_text(json.dumps(settings))
        token = json.loads((directory / "client.json").read_text())["access_token"]
        url = f"http://127.0.0.1:{port}"
        auth = {"Authorization": "Bearer " + token}
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "SELARK_CONFIG_DIR",
                "SELARK_DATA_DIR",
                "MIMO_API_KEY",
                "VIRTUAL_ENV",
                "CONDA_PREFIX",
            }
        }
        argv = [*command, "--config-dir", str(directory)]
        count = 0

        def rows():
            with contextlib.closing(sqlite3.connect(directory / "data/records.sqlite3")) as db:
                db.row_factory = sqlite3.Row
                return [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY started_at")]

        def matching(session):
            return next((r for r in rows() if r["session_id"] == session), None)

        def start():
            nonlocal service, logfile, count
            count += 1
            logfile = open(Path(tmp) / f"service-{count}.log", "w")  # noqa: SIM115 - closed by stop()
            service = subprocess.Popen(
                [*argv, "serve"], cwd=outside, env=env, stdout=logfile, stderr=subprocess.STDOUT
            )

            def ready():
                assert service.poll() is None, "Isolated service exited during startup"
                try:
                    response = httpx.get(url + "/health", trust_env=False, timeout=0.2)
                    return response.status_code == 200 and response.json()["version"] == __version__
                except httpx.RequestError:
                    return False

            wait_for(ready)

        def stop(*, crash=False):
            nonlocal service, logfile
            if service:
                service.kill() if crash else service.terminate()
                service.wait(timeout=5)
                service = None
            if logfile:
                logfile.close()
                logfile = None

        def post(prompt="fast", *, stream=False, session=None):
            return httpx.post(
                url + "/v1/chat/completions",
                headers=auth,
                json={
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": stream,
                    "session_id": session,
                    "max_tokens": 8,
                },
                trust_env=False,
                timeout=5,
            )

        try:
            start()
            for args in (
                ["--help"],
                ["models", "--json"],
                ["doctor", "--json"],
                ["limits", "show", "--json"],
            ):
                result = subprocess.run(
                    [*argv, *args], cwd=outside, env=env, capture_output=True, text=True, timeout=5
                )
                assert result.returncode == 0, result.stderr
            assert fixture.calls == []
            checks.append({"check": "cli_and_passive_checks", "passed": True, "upstream_calls": 0})
            assert post(session="ordinary").json()["usage"] == USAGE
            streamed = post(stream=True, session="stream")
            assert "[DONE]" in streamed.text
            wait_for(lambda: matching("stream")["delivery_status"] == "sent")
            assert matching("stream")["total_tokens"] == 14
            checks.append({"check": "ordinary_and_sse_over_real_local_sockets", "passed": True})

            fixture.entered.clear()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(post, "hold", session="active")
                assert fixture.entered.wait(2)
                second = pool.submit(post, "fast", session="queued")
                wait_for(
                    lambda: (
                        httpx.get(url + "/health", trust_env=False).json()["capacity"]["queued"]
                        == 1
                    )
                )
                rejected = post(session="overloaded")
                assert rejected.status_code == 429 and rejected.headers["retry-after"] == "1"
                timed_out = second.result(timeout=2)
                assert (
                    timed_out.status_code == 503
                    and timed_out.json()["error"]["code"] == "queue_timeout"
                )
                fixture.release.set()
                assert first.result(timeout=3).status_code == 200
            assert "overloaded" not in [r["session_id"] for r in rows()]
            wait_for(lambda: matching("queued") is not None)
            assert matching("queued")["upstream_attempts"] == 0
            checks.append({"check": "bounded_queue_overload_and_wait_timeout", "passed": True})

            before = time.monotonic()
            idle = post("idle", stream=True, session="idle")
            assert "upstream_timeout" in idle.text and "[DONE]" not in idle.text
            checks.append(
                {
                    "check": "actual_socket_read_idle_timeout",
                    "passed": True,
                    "elapsed_ms": round((time.monotonic() - before) * 1000, 2),
                }
            )
            before = time.monotonic()
            trickle = post("trickle", stream=True, session="trickle")
            assert "request_deadline_exceeded" in trickle.text and "[DONE]" not in trickle.text
            checks.append(
                {
                    "check": "trickling_stream_hits_total_deadline",
                    "passed": True,
                    "elapsed_ms": round((time.monotonic() - before) * 1000, 2),
                }
            )

            fixture.entered.clear()
            payload = json.dumps(
                {"messages": [{"role": "user", "content": "cancel"}], "session_id": "cancel"}
            ).encode()
            with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
                conn.sendall(
                    (
                        "POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                        f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(payload)}\r\n\r\n"
                    ).encode()
                    + payload
                )
                assert fixture.entered.wait(2)
            wait_for(lambda: matching("cancel") and matching("cancel")["status"] == "cancelled")
            assert matching("cancel")["total_tokens"] is None
            checks.append({"check": "ordinary_client_socket_disconnect", "passed": True})

            fixture.entered.clear()
            with (
                httpx.Client(trust_env=False, timeout=5) as client,
                client.stream(
                    "POST",
                    url + "/v1/chat/completions",
                    headers=auth,
                    json={
                        "messages": [{"role": "user", "content": "crash"}],
                        "stream": True,
                        "session_id": "crash",
                    },
                ) as response,
            ):
                assert response.status_code == 200 and fixture.entered.wait(2)
                assert next(response.iter_lines()).startswith("data:")
                stop(crash=True)
            before_restart = len(fixture.calls)
            start()
            assert len(fixture.calls) == before_restart
            assert matching("crash")["status"] == "interrupted"
            assert matching("crash")["total_tokens"] is None
            checks.append(
                {"check": "sigkill_restart_preserves_unknown_usage_without_retry", "passed": True}
            )

            fixture.entered.clear()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(post, "shutdown", session="shutdown")
                assert fixture.entered.wait(2)
                before = time.monotonic()
                stop()
                with contextlib.suppress(httpx.RequestError):
                    pending.result(timeout=3)
            assert matching("shutdown")["status"] == "interrupted"
            assert matching("shutdown")["error_code"] == "service_shutdown"
            checks.append(
                {
                    "check": "bounded_sigterm_grace_and_accounting",
                    "passed": True,
                    "elapsed_ms": round((time.monotonic() - before) * 1000, 2),
                }
            )
            before_restart = len(fixture.calls)
            start()
            assert len(fixture.calls) == before_restart
            assert post(session="after-recovery").status_code == 200
            wait_for(lambda: matching("after-recovery")["status"] == "success")
            ledger = rows()
            assert sum(r["total_tokens"] or 0 for r in ledger) == 56
            assert all(r["total_tokens"] is None for r in ledger if r["status"] != "success")
            checks.append(
                {
                    "check": "post_recovery_call_and_usage_totals",
                    "passed": True,
                    "known_total_tokens": 56,
                }
            )
            return {
                "verified_at": datetime.now(UTC).isoformat(),
                "version": __version__,
                "upstream": "temporary loopback fixture; invented credentials",
                "real_mimo_requests": 0,
                "simulated_upstream_requests": len(fixture.calls),
                "checks": checks,
                "request_records": ledger,
            }
        finally:
            stop()
            fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selark", help="Installed CLI path; default uses this Python -m selark")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run([args.selark] if args.selark else [sys.executable, "-m", "selark"])
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "request_records"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
