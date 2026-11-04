"""Paired loopback latency and core-process budgets; no real providers or profiles.

Each pair alternates direct/relay order. Percentiles use signed per-pair differences,
not differences between independent percentiles. The fixture returns fixed text;
these numbers exclude model generation, internet latency and concurrent queueing.
"""

import argparse
import contextlib
import json
import math
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from scripts.verify_stability import USAGE, chunk, completion
from selark import __version__
from selark.config import initialize
from selark.runtime import Runtime
from selark.schemas import ChatRequest
from selark.sse import SSEDecoder

# The reference idle window follows 610 relay requests, not a single warm-up call.
BUDGET = {"idle_rss_mib": 72, "idle_cpu_percent": 0.5, "relay_p95_ms": 10}
KEY = "fixture-only-key"
MESSAGES = [
    {"role": "system", "content": "Caller supplied instruction.\n"},
    {"role": "user", "content": "  Synthetic input 中文 😀  "},
]


def payload(stream):
    return {"messages": MESSAGES, "max_tokens": 16, "stream": stream, "session_id": "same"}


def distribution(values):
    ordered = sorted(values)
    return {
        name: round(ordered[math.ceil(q * len(ordered)) - 1], 4)
        for name, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
    }


def comparison(pairs):
    differences = [pair["relay_ms"] - pair["direct_ms"] for pair in pairs]
    return {
        "direct_ms": distribution([pair["direct_ms"] for pair in pairs]),
        "relay_ms": distribution([pair["relay_ms"] for pair in pairs]),
        "overhead_ms": distribution(differences),
        "negative_differences": sum(value < 0 for value in differences),
        "pairs": pairs,
    }


def process_stats(pid):
    root = Path("/proc") / str(pid)
    if not root.exists():
        return None
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    rss = next(
        int(line.split()[1]) * 1024
        for line in (root / "status").read_text().splitlines()
        if line.startswith("VmRSS:")
    )
    return {
        "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
        "rss_bytes": rss,
    }


class Fixture:
    def __init__(self):
        self.calls = 0
        self.errors = []
        expected = {
            stream: ChatRequest.model_validate(payload(stream)).upstream_payload("mimo-test")
            for stream in (False, True)
        }
        for document in expected.values():
            assert document["messages"] == MESSAGES
            assert (
                not {"instructions", "system", "input", "session_id", "connection_id"}
                & document.keys()
            )
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                # Avoid a delayed-ACK/Nagle artefact from separate headers/body writes.
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.errors.append("unexpected_metadata_request")
                self.send_error(400)

            def do_POST(self):
                document = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stream = document.get("stream")
                owner.calls += 1
                if (
                    self.path != "/v1/chat/completions"
                    or self.headers.get("api-key") != KEY
                    or type(stream) is not bool
                    or document != expected[stream]
                ):
                    owner.errors.append("upstream_request_changed")
                    self.send_error(400)
                    return
                body = (
                    (chunk() + chunk(final=True) + "data: [DONE]\n\n").encode()
                    if stream
                    else json.dumps(completion()).encode()
                )
                self.send_response(200)
                self.send_header(
                    "Content-Type", "text/event-stream" if stream else "application/json"
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def timed_request(client, body):
    start = time.perf_counter_ns()
    first = None
    with client.stream("POST", "/v1/chat/completions", json=body) as response:
        assert response.status_code == 200, "Synthetic request failed; no retry."
        if body["stream"]:
            decoder, text, usage, done = SSEDecoder(), [], None, False
            for line in response.iter_lines():
                raw = decoder.feed(line)
                if raw is None:
                    continue
                if raw == "[DONE]":
                    done = True
                    continue
                data = json.loads(raw)
                for choice in data.get("choices", []):
                    value = choice.get("delta", {}).get("content")
                    if value:
                        if first is None:
                            first = time.perf_counter_ns()
                        text.append(value)
                usage = data.get("usage") or usage
            assert done and "".join(text) == "OK" and usage == USAGE
            assert first is not None
        else:
            response.read()
            first = time.perf_counter_ns()
            assert response.json() == completion()
    end = time.perf_counter_ns()
    return {"ready_ms": (first - start) / 1e6, "complete_ms": (end - start) / 1e6}


def budget_checks(report):
    idle = report["idle"]
    return {
        "idle_rss": None
        if idle["rss_bytes"] is None
        else idle["rss_bytes"] / 1048576 <= BUDGET["idle_rss_mib"],
        "idle_cpu": None
        if idle["cpu_percent"] is None
        else idle["cpu_percent"] <= BUDGET["idle_cpu_percent"],
        "nonstream_p95": report["nonstream"]["overhead_ms"]["p95"] <= BUDGET["relay_p95_ms"],
        "stream_first_text_p95": report["stream_first_text"]["overhead_ms"]["p95"]
        <= BUDGET["relay_p95_ms"],
    }


def save_report(path, report):
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def run(*, samples=300, idle_seconds=30, report=None):
    report = {} if report is None else report
    with (
        tempfile.TemporaryDirectory(prefix="selark-relay-measure-") as temporary,
        contextlib.closing(Fixture()) as fixture,
    ):
        root = Path(temporary)
        profile = root / "profile"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        upstream = f"http://127.0.0.1:{fixture.server.server_port}"
        initialize(profile, KEY, base_url=upstream + "/v1", models=["mimo-test"], port=port)
        token = json.loads((profile / "client.json").read_text())["access_token"]
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("SELARK_") and k != "PYTHONPATH"
        }
        env["SELARK_DATA_DIR"] = str(profile / "data")
        with (root / "gateway.log").open("w") as log:
            entry = [sys.executable, "-m", "selark"]
            if hasattr(signal, "SIGUSR1"):
                # Capture all core thread stacks only on measurement failure.
                entry = [
                    sys.executable,
                    "-c",
                    "import faulthandler, signal; "
                    "faulthandler.register(signal.SIGUSR1, all_threads=True); "
                    "from selark.cli import main; raise SystemExit(main())",
                ]
            process = subprocess.Popen(
                [*entry, "--config-dir", str(profile), "serve"],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                with (
                    httpx.Client(
                        base_url=upstream, headers={"api-key": KEY}, trust_env=False, timeout=10
                    ) as direct,
                    httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={"Authorization": "Bearer " + token},
                        trust_env=False,
                        timeout=10,
                    ) as relay,
                ):
                    until = time.monotonic() + 15
                    while True:
                        assert process.poll() is None, "Isolated gateway exited before readiness."
                        try:
                            if relay.get("/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert time.monotonic() < until, "Isolated gateway did not start."
                        time.sleep(0.02)
                    assert fixture.calls == 0 and not fixture.errors
                    report.update(
                        {
                            "version": __version__,
                            "python": platform.python_version(),
                            "platform": platform.system(),
                            "machine": platform.machine(),
                            "concurrency": 1,
                            "samples_per_mode": samples,
                            "warmup_pairs_per_mode": 5,
                            "real_provider_requests": 0,
                            "budgets": BUDGET,
                        }
                    )
                    for stream in (False, True):
                        wire = ChatRequest.model_validate(payload(stream)).upstream_payload(
                            "mimo-test"
                        )
                        ready_pairs, complete_pairs = [], []
                        report["progress"] = {"stream": stream, "pair": 0}
                        report["partial_pairs"] = ready_pairs
                        for i in range(samples + 5):
                            order = ("direct", "relay") if i % 2 == 0 else ("relay", "direct")
                            times = {}
                            for route in order:
                                report["progress"].update(pair=i, route=route)
                                times[route] = timed_request(
                                    direct if route == "direct" else relay,
                                    wire if route == "direct" else payload(stream),
                                )
                            if i >= 5:
                                ready_pairs.append({k + "_ms": times[k]["ready_ms"] for k in order})
                                complete_pairs.append(
                                    {k + "_ms": times[k]["complete_ms"] for k in order}
                                )
                        report["stream_first_text" if stream else "nonstream"] = comparison(
                            ready_pairs
                        )
                        if stream:
                            report["stream_complete"] = comparison(complete_pairs)
                # Drain the final receipt, then measure only the core PID while idle.
                report["progress"] = {"phase": "idle"}
                time.sleep(1.1)
                before_calls = fixture.calls
                start_stats, start = process_stats(process.pid), time.perf_counter()
                time.sleep(idle_seconds)
                elapsed, end_stats = time.perf_counter() - start, process_stats(process.pid)
                assert process.poll() is None, "Gateway exited during measurement."
                assert fixture.calls == before_calls and not fixture.errors
                cpu = (
                    None
                    if start_stats is None or end_stats is None
                    else end_stats["cpu_seconds"] - start_stats["cpu_seconds"]
                )
                report["idle"] = {
                    "seconds": round(elapsed, 4),
                    "cpu_seconds": cpu,
                    "cpu_percent": None if cpu is None else 100 * cpu / elapsed,
                    "rss_bytes": None
                    if start_stats is None or end_stats is None
                    else max(start_stats["rss_bytes"], end_stats["rss_bytes"]),
                    "upstream_requests": fixture.calls - before_calls,
                }
            except BaseException:
                if process.poll() is None and hasattr(signal, "SIGUSR1"):
                    process.send_signal(signal.SIGUSR1)
                    time.sleep(0.05)
                try:
                    with httpx.Client(trust_env=False, timeout=1) as health:
                        report["failure_health"] = health.get(
                            f"http://127.0.0.1:{port}/health"
                        ).json()
                except (httpx.HTTPError, ValueError):
                    report["failure_health"] = None
                report.update(
                    failed=True,
                    gateway_exit_code=process.poll(),
                    observed_upstream_requests=fixture.calls,
                    fixture_errors=fixture.errors,
                    gateway_log_tail=(root / "gateway.log").read_text()[-16000:],
                )
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            # Uvicorn re-raises SIGTERM after completing its lifespan cleanup.
            assert process.returncode in (0, -signal.SIGTERM), process.returncode
        relayed = 2 * (samples + 5)
        assert fixture.calls == 2 * relayed and not fixture.errors, (fixture.calls, 2 * relayed)
        with Runtime(profile, readonly=True, data_path=profile / "data") as runtime:
            summary = runtime.store.summary({})["summary"]
            assert summary["requests"] == relayed
            assert summary["known_tokens"]["total_tokens"] == relayed * 14
            assert not list((runtime.data_dir / "pending").glob("*.json"))
        report.update(
            fake_direct_generations=relayed,
            fake_relay_generations=relayed,
            extra_generations=fixture.calls - 2 * relayed,
            forwarded_payloads_unchanged=True,
            reported_relay_tokens=relayed * 14,
        )
        report["budget_checks"] = budget_checks(report)
        report["reference_sample_size"] = samples >= 300 and idle_seconds >= 30
        report.pop("progress", None)
        report.pop("partial_pairs", None)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--idle-seconds", type=float, default=30)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check-budget",
        action="store_true",
        help="Fail for any missed/unavailable budget; requires 300 pairs/mode and 30 s idle.",
    )
    args = parser.parse_args()
    if not 1 <= args.samples <= 10000 or not 0 < args.idle_seconds <= 60:
        parser.error("samples must be in [1, 10000], idle-seconds in (0, 60]")
    if args.check_budget and (args.samples < 300 or args.idle_seconds < 30):
        parser.error("budget checks require at least 300 samples and 30 idle seconds")
    report = {}
    try:
        report = run(samples=args.samples, idle_seconds=args.idle_seconds, report=report)
    except Exception as error:
        report.update(failed=True, error_type=type(error).__name__)
        # Replace an earlier success at the same path; stale results must not hide a failure.
        save_report(args.output, report)
        raise
    save_report(args.output, report)
    brief = {
        k: ({x: y for x, y in v.items() if x != "pairs"} if isinstance(v, dict) else v)
        for k, v in report.items()
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    return int(args.check_budget and not all(v is True for v in report["budget_checks"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
