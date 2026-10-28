"""Explicit paid acceptance: six concurrent calls, one output-limit call, one disconnect.

Requires three explicit connection::model targets, an existing second client profile,
--execute, and a never-used private budget file. At most eight generation attempts.
No retries, model discovery, configuration edits, or token creation/revocation.
"""

import argparse
import asyncio
import contextlib
import hashlib
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
from selark.storage import Store, token_digest


async def run(args):
    directory = config_directory(args.config_dir)
    profiles = [
        load_config(directory / "client.json", ClientConfig),
        load_config(args.secondary_client, ClientConfig),
    ]
    assert profiles[0].url == profiles[1].url
    targets = [target.split("::", 1) for target in args.target]
    if len(targets) != 3 or any(len(t) != 2 or not all(t) for t in targets):
        raise ValueError("Exactly three connection::model targets required")
    assert not os.environ.get("VIRTUAL_ENV") and not os.environ.get("CONDA_PREFIX")
    database = data_directory(directory) / "records.sqlite3"
    files = [
        directory / "server.json",
        directory / "client.json",
        directory / "connections.json",
        args.secondary_client,
    ]
    before_files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    def query(sql, values=()):
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(sql, values)]

    last = query("SELECT COALESCE(MAX(rowid),0) AS last FROM requests")[0]["last"]
    old_requests = query("SELECT * FROM requests WHERE rowid<=? ORDER BY rowid", (last,))
    old_tokens = query("SELECT * FROM tokens ORDER BY id")
    old_imports = query("SELECT * FROM imports ORDER BY content_hash")
    budget = {"maximum": 8, "attempted_requests": 0}
    fd = os.open(args.budget, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(budget, output)
    report = {
        "version": __version__,
        "started_at": datetime.now(UTC).isoformat(),
        "maximum_real_requests": 8,
        "model_generation_retries": 0,
        "working_directory": "temporary directory outside repository",
        "activated_environment": False,
        "calls": [],
        "capacity_samples": [],
        "original_requests": len(old_requests),
    }
    gate = asyncio.Lock()

    def save():
        records = [
            Store.public_record(r)
            for r in query("SELECT * FROM requests WHERE rowid>? ORDER BY rowid", (last,))
        ]
        report.update(attempted_http_requests=budget["attempted_requests"], server_requests=records)
        report["real_upstream_attempts"] = sum(r["upstream_attempts"] or 0 for r in records)
        report["known_tokens"] = {
            field: sum(r[field] for r in records if r[field] is not None)
            if any(r[field] is not None for r in records)
            else None
            for field in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        report["requests_missing_complete_usage"] = sum(not r["usage_known"] for r in records)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async def record(identity):
        async with asyncio.timeout(10):
            while True:
                rows = query("SELECT * FROM requests WHERE request_id=?", (identity,))
                if rows and rows[0]["status"] != "running":
                    return Store.public_record(rows[0])
                await asyncio.sleep(0.03)

    async with httpx.AsyncClient(base_url=profiles[0].url, trust_env=False, timeout=150) as client:

        async def health():
            try:
                response = await client.get("/health", timeout=0.5)
                return response.json() if response.status_code == 200 else None
            except httpx.RequestError:
                return None

        assert await health() is None, "Stop the existing service first"

        async def call(index, target, *, streaming=False, mode="parallel"):
            async with gate:
                assert budget["attempted_requests"] < budget["maximum"]
                budget["attempted_requests"] += 1
                _write_private(args.budget, budget)
            connection, model = target
            profile = profiles[index % 2]
            messages = [{"role": "user", "content": "Reply with exactly OK."}]
            if streaming:
                messages = [
                    {"role": "user", "content": "Remember the digit 7."},
                    {"role": "assistant", "content": "7"},
                    {"role": "user", "content": "Repeat that digit only."},
                ]
            if mode in {"output_limit", "disconnect"}:
                messages = [{"role": "user", "content": "Count from 1 to 100 separated by spaces."}]
            payload = {
                "connection_id": connection,
                "model": model,
                "messages": messages,
                "stream": streaming,
                "max_tokens": 1 if mode == "output_limit" else 64 if mode == "disconnect" else 16,
                "session_id": "api-stress-0.4.1" + ("" if mode == "parallel" else "-" + mode),
            }
            result = {
                "index": index,
                "mode": mode,
                "connection_id": connection,
                "model": model,
                "stream": streaming,
                "client_profile_index": index % 2,
                "max_tokens": payload["max_tokens"],
            }
            identity = None
            observed_usage, reason = None, None
            done = disconnected = False
            started = time.monotonic()
            try:
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer " + profile.access_token.get_secret_value()},
                    json=payload,
                ) as response:
                    identity = response.headers.get("x-request-id")
                    result["http_status"] = response.status_code
                    result["response_connection"] = response.headers.get("x-selark-connection")
                    if response.status_code != 200:
                        data = json.loads(await response.aread())
                        result["error_code"] = data.get("error", {}).get("code")
                    elif streaming:
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            raw = line[6:]
                            if raw == "[DONE]":
                                done = True
                                break
                            data = json.loads(raw)
                            if "error" in data:
                                result["error_code"] = data["error"].get("code")
                                break
                            usage = reported_usage(data)
                            if usage is not None:
                                observed_usage = usage
                            for choice in data.get("choices", []):
                                reason = choice.get("finish_reason") or reason
                            if mode == "disconnect" and any(
                                c.get("delta", {}).get("content") for c in data.get("choices", [])
                            ):
                                disconnected = True
                                break
                    else:
                        data = json.loads(await response.aread())
                        observed_usage = reported_usage(data)
                        reason = data["choices"][0]["finish_reason"]
                        done = True
                result.update(
                    client_duration_ms=round((time.monotonic() - started) * 1000, 2),
                    finish_reason=reason,
                    received_done=done,
                    client_closed_after_first_text=disconnected,
                )
                if identity:
                    row = await record(identity)
                    result["record"] = row
                    identity_match = (
                        row["caller_id"]
                        == query(
                            "SELECT id FROM tokens WHERE digest=?",
                            (token_digest(profile.access_token.get_secret_value()),),
                        )[0]["id"]
                    )
                    result["identity_matches_authentication"] = identity_match
                    result["usage_matches_response"] = row["reported_usage"] == observed_usage
                    if mode == "disconnect":
                        result["passed"] = (
                            disconnected
                            and identity_match
                            and row["status"] in {"cancelled", "success"}
                        )
                    else:
                        result["passed"] = (
                            done
                            and row["status"] == "success"
                            and result["usage_matches_response"]
                            and identity_match
                        )
                        if mode == "output_limit":
                            result["passed"] &= reason == "length" and row["completion_tokens"] == 1
                    result["passed"] &= (
                        row["connection_id"] == connection and row["upstream_attempts"] == 1
                    )
            except Exception as exc:
                result.update(passed=False, client_error_type=type(exc).__name__)
            async with gate:
                report["calls"].append(result)
                save()
            print(
                json.dumps({k: v for k, v in result.items() if k != "record"}, ensure_ascii=False),
                flush=True,
            )
            return result

        with tempfile.TemporaryDirectory(prefix="selark-stress-live-") as tmp:
            fd = os.open(Path(tmp) / "service.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as log:
                service = subprocess.Popen(
                    [args.selark, "--config-dir", str(directory), "serve"],
                    cwd=tmp,
                    stdout=log,
                    stderr=log,
                )
                try:
                    async with asyncio.timeout(8):
                        while not (state := await health()):
                            assert service.poll() is None
                            await asyncio.sleep(0.03)
                    assert state["version"] == __version__
                    jobs = [
                        asyncio.create_task(call(i, target, streaming=i >= 3))
                        for i, target in enumerate(targets * 2)
                    ]
                    while not all(job.done() for job in jobs):
                        h = await health()
                        if h:
                            sample = h["capacity"]
                            if (
                                not report["capacity_samples"]
                                or sample != report["capacity_samples"][-1]
                            ):
                                report["capacity_samples"].append(sample)
                        await asyncio.sleep(0.02)
                    outcomes = await asyncio.gather(*jobs)
                    if all(r.get("passed") for r in outcomes):
                        limit = await call(6, targets[1], mode="output_limit")
                        if limit.get("passed"):
                            await call(7, targets[0], streaming=True, mode="disconnect")
                    report["final_health"] = await health()
                finally:
                    service.terminate()
                    try:
                        service.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        service.kill()
                        service.wait(timeout=5)
                    report["service_after"] = "stopped"
                    report["finished_at"] = datetime.now(UTC).isoformat()
                    report["original_configuration_unchanged"] = all(
                        hashlib.sha256(p.read_bytes()).hexdigest() == digest
                        for p, digest in before_files.items()
                    )
                    report["original_requests_unchanged"] = old_requests == query(
                        "SELECT * FROM requests WHERE rowid<=? ORDER BY rowid", (last,)
                    )
                    report["token_rows_and_revocations_unchanged"] = old_tokens == query(
                        "SELECT * FROM tokens ORDER BY id"
                    )
                    report["history_imports_unchanged"] = old_imports == query(
                        "SELECT * FROM imports ORDER BY content_hash"
                    )
                    save()
        assert await health() is None
    report["peak_active"] = max(s["active"] for s in report["capacity_samples"])
    report["peak_queued"] = max(s["queued"] for s in report["capacity_samples"])
    report["all_checks_passed"] = (
        len(report["calls"]) == 8
        and all(c.get("passed") for c in report["calls"])
        and report["peak_active"] == 4
        and report["peak_queued"] >= 1
    )
    save()
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"calls", "server_requests", "capacity_samples"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["all_checks_passed"]:
        raise RuntimeError(
            "Some checks failed; results saved, do not automatically retry paid requests"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", required=True, action="store_true")
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--secondary-client", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--selark", default="selark")
    asyncio.run(run(parser.parse_args()))
