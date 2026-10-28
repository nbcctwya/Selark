#!/usr/bin/env python3
"""Opt-in acceptance: starts/stops Selark and makes exactly four chat requests."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from selark.config import ClientConfig, ServerConfig, config_directory, load_config

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-requests", action="store_true", required=True)
    parser.add_argument("--config-dir", type=Path, default=config_directory())
    parser.add_argument("--output", type=Path, default=ROOT / "docs/live-verification.json")
    parser.add_argument("--include-probe", type=Path)
    args = parser.parse_args()
    directory = args.config_dir.expanduser().resolve()
    server = load_config(directory / "server.json", ServerConfig)
    client = load_config(directory / "client.json", ClientConfig)
    secrets = [server.api_key.get_secret_value(), server.access_token.get_secret_value()]
    fingerprint = hashlib.sha256((directory / "server.json").read_bytes()).digest()
    results = []
    logs = []
    process = None
    log_handle = None
    started_count = 0
    failure = None

    def clean(text):
        for value in secrets:
            text = text.replace(value, "[REDACTED]")
        return text

    with tempfile.TemporaryDirectory(prefix="selark-live-") as temp_name:
        temp = Path(temp_name)
        # Real CLI calls must work with no server.json available in their configuration directory.
        client_only = temp / "client-only"
        client_only.mkdir(mode=0o700)
        shutil.copy2(directory / "client.json", client_only / "client.json")
        cli = [sys.executable, "-m", "selark", "--config-dir", str(client_only)]
        env = os.environ.copy()
        env.pop("MIMO_API_KEY", None)
        env.pop("PYTHONPATH", None)

        def run(name, command, expected=0, cwd=ROOT):
            start = time.monotonic()
            result = subprocess.run(
                command, cwd=cwd, env=env, capture_output=True, text=True, timeout=90
            )
            item = {
                "check": name,
                "exit_code": result.returncode,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "stdout": clean(result.stdout.strip()),
                "stderr": clean(result.stderr.strip()),
            }
            for line in result.stderr.splitlines():
                try:
                    data = json.loads(line)
                    if "usage" in data:
                        item["usage"] = data["usage"]
                        item["model"] = data.get("model")
                except (ValueError, TypeError):
                    pass
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            if result.returncode != expected:
                raise RuntimeError(f"{name}: expected exit {expected}, got {result.returncode}")
            return item

        def start_server():
            nonlocal process, log_handle, started_count
            with httpx.Client(trust_env=False, timeout=1) as http:
                try:
                    http.get(client.url + "/health")
                except httpx.RequestError:
                    pass
                else:
                    raise RuntimeError(
                        "Configured port is already in use; stop existing server first."
                    )
            started_count += 1
            log_path = temp / f"server-{started_count}.log"
            log_handle = log_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                [sys.executable, "-m", "selark", "--config-dir", str(directory), "serve"],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=ROOT,
                env=env,
            )
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Selark exited before becoming ready.")
                try:
                    with httpx.Client(trust_env=False, timeout=1) as http:
                        response = http.get(client.url + "/health")
                        if response.status_code == 200:
                            results.append(
                                {
                                    "check": f"health_start_{started_count}",
                                    "http_status": 200,
                                    "body": response.json(),
                                }
                            )
                            return
                except httpx.RequestError:
                    pass
                time.sleep(0.05)
            raise RuntimeError("Selark startup timed out.")

        def stop_server():
            nonlocal process, log_handle
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process = None
            if log_handle is not None:
                log_handle.close()
                log_handle = None

        try:
            start_server()
            run("cli_models", cli + ["models", "--json"])
            with httpx.Client(trust_env=False, timeout=5) as http:
                wrong = http.post(
                    client.url + "/v1/chat/completions",
                    headers={"Authorization": "Bearer invalid"},
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
                bad = http.post(
                    client.url + "/v1/chat/completions",
                    headers={"Authorization": "Bearer " + secrets[1]},
                    json={"messages": []},
                )
                assert wrong.status_code == 401 and bad.status_code == 400
                results.append(
                    {
                        "check": "local_auth_and_validation",
                        "wrong_token": 401,
                        "invalid_messages": 400,
                        "upstream_requests": 0,
                    }
                )
            run(
                "cli_regular",
                cli + ["ask", "Reply with exactly: Selark HTTP OK", "--max-tokens", "32"],
            )
            run(
                "cli_stream",
                cli + ["ask", "请只回复：Selark 流式调用成功。", "--stream", "--max-tokens", "32"],
            )
            shutil.copy2(ROOT / "examples/summarize.py", temp / "summarize.py")
            shutil.copy2(ROOT / "examples/article.txt", temp / "article.txt")
            run(
                "standalone_example",
                [
                    shutil.which("python3"),
                    "-I",
                    str(temp / "summarize.py"),
                    "--config",
                    str(client_only / "client.json"),
                    "--input-file",
                    str(temp / "article.txt"),
                ],
                cwd=temp,
            )
            stop_server()
            start_server()
            assert fingerprint == hashlib.sha256((directory / "server.json").read_bytes()).digest()
            run(
                "restart_multiturn",
                cli
                + ["ask", "--messages", str(ROOT / "examples/messages.json"), "--max-tokens", "32"],
            )
            stop_server()
            offline = run(
                "offline_cli_ask", cli + ["ask", "Do not contact any provider"], expected=1
            )
            assert "无法连接 Selark" in offline["stderr"]
            run("offline_cli_models", cli + ["models"], expected=1)
            assert not (client_only / "server.json").exists()
        except Exception as exc:
            failure = f"{type(exc).__name__}: {clean(str(exc))}"
        finally:
            stop_server()
            for path in sorted(temp.glob("server-*.log")):
                for line in path.read_text().splitlines():
                    try:
                        logs.append(json.loads(clean(line)))
                    except ValueError:
                        if "ERROR" in line or "Traceback" in line:
                            results.append({"check": "server_diagnostic", "message": clean(line)})

    report = {"checks": results, "server_requests": logs, "failure": failure}
    if args.include_probe:
        report["initial_probe"] = json.loads(args.include_probe.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(clean(json.dumps(report, ensure_ascii=False, indent=2)) + "\n")
    print(f"Saved sanitized evidence to {args.output}")
    if failure:
        print(failure, file=sys.stderr)
        return 1
    print("Live acceptance passed; Selark has been stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
