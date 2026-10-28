"""Explicit paid acceptance: two short CLI calls per selected connection.

Requires --execute, a never-used private budget path, and explicit connection::model targets.
Uses existing client credentials without printing or copying them. Never retries generation.
The service must be stopped before running; this script stops only the process it starts.
"""

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from selark import __version__
from selark.config import (
    ClientConfig,
    _write_private,
    config_directory,
    data_directory,
    load_config,
)
from selark.providers.mimo import reported_usage
from selark.storage import Store


def wait_for(function, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(0.03)
    raise RuntimeError("Acceptance condition timed out")


def run(args):
    directory = config_directory(args.config_dir)
    profile = load_config(directory / "client.json", ClientConfig)
    database = data_directory(directory) / "records.sqlite3"
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        original_row = db.execute("SELECT COALESCE(MAX(rowid),0) FROM requests").fetchone()[0]
    targets = [target.split("::", 1) for target in args.target]
    if any(len(t) != 2 or not all(t) for t in targets) or len(targets) > 4:
        raise ValueError("Provide 1–4 explicit connection::model targets")
    if os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX"):
        raise RuntimeError("Run from a fresh terminal without an activated environment")
    fd = os.open(args.budget, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump({"attempted_cli_requests": 0, "maximum": 8}, output)
    budget = {"attempted_cli_requests": 0, "maximum": 8}
    report = {
        "version": __version__,
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "real MiMo, DeepSeek and explicitly configured relay only",
        "working_directory": "temporary directory outside repository",
        "activated_environment": False,
        "calls": [],
        "model_generation_retries": 0,
    }

    def health():
        try:
            response = httpx.get(profile.url + "/health", trust_env=False, timeout=0.3)
            return response.json() if response.status_code == 200 else None
        except httpx.RequestError:
            return None

    def record(identity):
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM requests WHERE request_id=?", (identity,)).fetchone()
            return Store.public_record(row) if row and row["status"] != "running" else None

    def save():
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            report["server_requests"] = [
                Store.public_record(row)
                for row in db.execute(
                    "SELECT * FROM requests WHERE rowid>? ORDER BY rowid", (original_row,)
                )
            ]
        report["attempted_cli_requests"] = budget["attempted_cli_requests"]
        report["real_upstream_requests"] = sum(
            row["upstream_attempts"] or 0 for row in report["server_requests"]
        )
        report["known_tokens"] = {
            key: sum(row[key] for row in report["server_requests"] if row[key] is not None)
            if any(row[key] is not None for row in report["server_requests"])
            else None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        report["requests_missing_complete_usage"] = sum(
            not row["usage_known"] for row in report["server_requests"]
        )
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    if health() is not None:
        raise RuntimeError("Stop the existing service before this explicit acceptance")
    with tempfile.TemporaryDirectory(prefix="selark-multi-live-") as tmp:
        outside = Path(tmp)
        message_file = outside / "messages.json"
        message_file.write_text(
            json.dumps(
                [
                    {"role": "user", "content": "Remember the digit 7."},
                    {"role": "assistant", "content": "7"},
                    {"role": "user", "content": "Repeat that digit only."},
                ]
            )
        )
        fd = os.open(outside / "service.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as log:
            command = [args.selark, "--config-dir", str(directory)]
            service = subprocess.Popen([*command, "serve"], cwd=outside, stdout=log, stderr=log)
            try:
                status = wait_for(health)
                assert status["version"] == __version__
                for connection, model in targets:
                    for streaming in (False, True):
                        assert budget["attempted_cli_requests"] < budget["maximum"]
                        budget["attempted_cli_requests"] += 1
                        _write_private(args.budget, budget)
                        options = [
                            "ask",
                            "--connection",
                            connection,
                            "--model",
                            model,
                            "--max-tokens",
                            "16",
                            "--session",
                            "multiconnection-0.4-verification",
                        ]
                        options += (
                            ["--messages", str(message_file), "--stream"]
                            if streaming
                            else ["Reply with exactly OK.", "--json"]
                        )
                        if connection == "mimo-default":
                            options.remove("--connection")
                            options.remove(connection)
                        response = subprocess.run(
                            [*command, *options],
                            cwd=outside,
                            text=True,
                            capture_output=True,
                            timeout=150,
                        )
                        call = {
                            "connection_id": connection,
                            "requested_model": model,
                            "stream": streaming,
                            "multiturn": streaming,
                            "exit_code": response.returncode,
                        }
                        details = next(
                            (
                                json.loads(line)
                                for line in response.stderr.splitlines()
                                if line.startswith('{"request_id":')
                            ),
                            None,
                        )
                        if details:
                            row = wait_for(lambda identity=details["request_id"]: record(identity))
                            call["record"] = row
                            call["usage_matches_cli_response"] = (
                                reported_usage({"usage": details["usage"]}) == row["reported_usage"]
                            )
                            call["connection_matches"] = (
                                row["connection_id"] == connection == details["connection_id"]
                            )
                            if streaming:
                                call["expected_text_present"] = "7" in response.stdout
                            else:
                                data = json.loads(response.stdout)
                                call["expected_text_present"] = (
                                    "OK" in data["choices"][0]["message"]["content"]
                                )
                                call["usage_matches_cli_response"] &= (
                                    reported_usage(data) == row["reported_usage"]
                                )
                            call["passed"] = (
                                response.returncode == 0
                                and row["status"] == "success"
                                and call["connection_matches"]
                                and call["usage_matches_cli_response"]
                                and call["expected_text_present"]
                            )
                        else:
                            call["passed"] = False
                            # Save only sanitized CLI errors, never raw upstream bodies.
                            call["error"] = response.stderr.strip()[:500]
                        report["calls"].append(call)
                        save()
                        print(
                            json.dumps(
                                {k: v for k, v in call.items() if k != "record"}, ensure_ascii=False
                            ),
                            flush=True,
                        )
                        if not call["passed"]:
                            break  # Do not spend another request on a failing connection.
                report["finished_at"] = datetime.now(UTC).isoformat()
            finally:
                service.terminate()
                try:
                    service.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=5)
                report["service_after"] = "stopped"
                save()
    assert health() is None
    if not all(c["passed"] for c in report["calls"]) or len(report["calls"]) != len(targets) * 2:
        raise RuntimeError(
            "Some real acceptance checks failed; progress is saved, no automatic retry"
        )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"calls", "server_requests"}},
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--selark", default="selark")
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
