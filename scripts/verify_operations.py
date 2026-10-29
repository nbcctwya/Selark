"""0.7.1 installed operations and history performance acceptance, with loopback fakes only."""

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from verify_stability import Fixture, wait_for

from selark import __version__
from selark.config import initialize
from selark.overview import overview
from selark.runtime import Runtime
from selark.storage import utc_now


def run(command, *, history_rows=20000):
    fixture = Fixture()
    checks = []
    with tempfile.TemporaryDirectory(prefix="selark-operations-") as temporary:
        root = Path(temporary)
        profile = root / "instance"
        outside = root / "outside"
        outside.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        initialize(
            profile,
            "test-stability-upstream-key",
            base_url=f"http://127.0.0.1:{fixture.server.server_port}/v1",
            models=["mimo-test"],
            default_model="mimo-test",
            port=port,
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("SELARK_") and key != "PYTHONPATH"
        }
        env["XDG_CONFIG_HOME"] = str(root / "xdg")
        env["SELARK_DATA_DIR"] = str(profile / "data")

        def cli(*args, json_output=True):
            result = subprocess.run(
                [*command, "--config-dir", str(profile), *args],
                env=env,
                cwd=outside,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode:
                raise AssertionError(f"{args[:2]} failed: {result.stderr[:1200]}")
            return json.loads(result.stdout) if json_output else result.stdout

        try:
            started = cli("service", "start", "--backend", "process", "--json")
            assert started["state"] == "running" and started["backend"] == "process"
            pid = started["pid"]
            assert cli("service", "start", "--backend", "process", "--json")["pid"] == pid
            checks.append("background start confirms own health and repeated start is idempotent")
            assert cli("status", "--json")["service"]["version"] == __version__
            assert not fixture.calls
            cli("ask", "ordinary", "--max-tokens", "16", json_output=False)
            cli("ask", "stream", "--stream", "--max-tokens", "16", json_output=False)
            assert len(fixture.calls) == 2
            usage = cli("usage", "--json")["summary"]
            assert usage["known_tokens"]["total_tokens"] == 28
            checks.append("two explicit fake generations recorded once with 28 tokens")
            backup = cli("backup", "--json")["backup"]
            assert cli("backup", "verify", backup, "--json")["ok"]
            drill = cli("backup", "drill", backup, "--json")
            assert (
                drill["isolated_copy_removed"]
                and drill["usage"]["known_tokens"]["total_tokens"] == 28
            )
            assert cli("usage", "--json")["summary"] == usage
            checks.append(
                "online backup hash verification and isolated recovery preserve live ledger"
            )
            logs = cli("logs", "--lines", "20", "--json")["lines"]
            assert any('"upstream_attempts": 1' in line for line in logs)
            token = json.loads((profile / "client.json").read_bytes())["access_token"]
            assert "test-stability-upstream-key" not in str(logs) and token not in str(logs)
            checks.append("persistent logs contain request metadata and no fake credentials")
            restarted = cli("service", "restart", "--backend", "process", "--json")
            assert restarted["state"] == "running" and restarted["pid"] != pid
            assert cli("usage", "--json")["summary"] == usage
            assert len(fixture.calls) == 2
            checks.append("graceful restart preserves history without regenerating")
            os.kill(restarted["pid"], signal.SIGKILL)
            wait_for(lambda: cli("service", "status", "--json")["pid"] is None)
            assert cli("service", "start", "--backend", "process", "--json")["state"] == "running"
            assert len(fixture.calls) == 2
            assert cli("service", "stop", "--json")["pid"] is None
            assert cli("service", "stop", "--json")["pid"] is None
            checks.append("crash restart and repeated stop are safe without extra generation")
        finally:
            with contextlib.suppress(Exception):
                cli("service", "stop", "--json")
            fixture.close()

        benchmark = root / "benchmark"
        initialize(benchmark, "fake-benchmark", models=["mimo-test"], default_model="mimo-test")
        with Runtime(benchmark, data_path=benchmark / "data") as runtime:
            timestamp = utc_now()
            with runtime.store.transaction() as db:
                db.executemany(
                    "INSERT INTO requests(request_id,started_at,caller_id,connection_id,model,"
                    "purpose,status,upstream_attempts,usage_final,prompt_tokens,completion_tokens,"
                    "total_tokens) VALUES(?,?,'legacy','mimo-default','mimo-test',"
                    "'chat','success',1,1,12,2,14)",
                    ((f"benchmark-{i}", timestamp) for i in range(history_rows)),
                )
        # Use no ambient override for this read-only benchmark.
        saved = os.environ.pop("SELARK_DATA_DIR", None)
        try:
            samples = []
            for _ in range(3):
                beginning = time.perf_counter()
                result = overview(benchmark, offline=True)
                samples.append(round(time.perf_counter() - beginning, 4))
                assert (
                    result["ok"]
                    and result["usage"]["known_tokens"]["total_tokens"] == 14 * history_rows
                )
            assert max(samples) < 10, "20k-row overview exceeded a generous CI bound"
        finally:
            if saved is not None:
                os.environ["SELARK_DATA_DIR"] = saved
        checks.append("offline history overview is consistent on every repeated read")
        return {
            "version": __version__,
            "passed": True,
            "checks": checks,
            "real_provider_requests": 0,
            "oauth_operations": 0,
            "fake_generations": 2,
            "known_tokens": 28,
            "history_rows": history_rows,
            "overview_seconds": samples,
            "service_backend": "process",
            "systemd": "covered by unit simulation",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selark", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = [str(args.selark)] if args.selark else [sys.executable, "-m", "selark"]
    report = run(command)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
