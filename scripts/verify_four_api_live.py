"""Explicit, bounded live concurrency acceptance for four user-owned API connections.

At most seven requests per provider, 28 total. No retries or model fallback.
Credentials remain in memory; only temporary local callers and ledgers are created.
The work directory must be new, so rerunning cannot reset an existing request budget.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import socket
import tempfile
import time
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from verify_resource_matrix import serve, wait_for

from selark import __version__
from selark.app import create_app
from selark.config import ServerConfig, _write_private, config_directory, initialize, load_config
from selark.connections import ConnectionConfig, ConnectionsConfig, legacy_connection
from selark.runtime import Runtime
from selark.storage import utc_now

MAX_REQUESTS = 28
PER_CONNECTION = 7


class LiveGuard:
    """Reserve before send, reject unapproved destinations and never refund a failed attempt."""

    def __init__(self, directory, connections):
        self.directory = directory
        self.allowed = {c.base_url + "/chat/completions": c for c in connections}
        if len(self.allowed) != len(connections):
            raise ValueError("Live targets must have distinct approved endpoints")
        self.counts = Counter()
        self.events = []
        self.requests = {}
        self.stopped_reason = None
        self.started = time.monotonic()
        self.lock = asyncio.Lock()
        self.budget_path = directory / "budget.json"
        with self.budget_path.open("x"):
            pass
        os.chmod(self.budget_path, 0o600)
        self.save()

    def save(self):
        _write_private(
            self.budget_path,
            {
                "maximum": MAX_REQUESTS,
                "per_connection_maximum": PER_CONNECTION,
                "attempted_requests": sum(self.counts.values()),
                "attempts_by_connection": dict(self.counts),
                "events": self.events,
                "stopped_reason": self.stopped_reason,
            },
        )

    def observe_result(self, result):
        usage = result.get("usage")
        output = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if type(output) is int and output > result["output_cap"]:
            self.stopped_reason = "reported_output_exceeded_requested_cap"
            (self.directory / "STOP").touch(mode=0o600)
            self.save()

    async def before(self, request):
        # ConnectError communicates that a rejected request was never sent.
        async with self.lock:
            connection = self.allowed.get(str(request.url))
            if request.method != "POST" or connection is None:
                raise httpx.ConnectError("Unapproved acceptance destination", request=request)
            data = json.loads(request.content)
            limit = 256 if connection.provider == "openai" else 64
            output = data.get("max_completion_tokens", data.get("max_tokens"))
            credential_header = "api-key" if connection.provider == "mimo" else "authorization"
            expected = connection.api_key.get_secret_value()
            if credential_header == "authorization":
                expected = "Bearer " + expected
            if (
                data.get("model") != connection.default_model
                or type(output) is not int
                or not 1 <= output <= limit
                or len(request.content) > 1200
                or "tools" in data
                or request.headers.get(credential_header) != expected
                or self.counts[connection.connection_id] >= PER_CONNECTION
                or sum(self.counts.values()) >= MAX_REQUESTS
                or time.monotonic() - self.started > 600
                or (self.directory / "STOP").exists()
            ):
                raise httpx.ConnectError(
                    "Acceptance guard stopped request before send", request=request
                )
            self.counts[connection.connection_id] += 1
            event = {
                "connection": connection.connection_id,
                "model": data["model"],
                "stream": data["stream"],
                "output_cap": output,
                "token_parameter": (
                    "max_completion_tokens" if "max_completion_tokens" in data else "max_tokens"
                ),
                "request_body_sha256": hashlib.sha256(request.content).hexdigest(),
                "start_ms": round((time.monotonic() - self.started) * 1000, 3),
            }
            self.events.append(event)
            self.requests[id(request)] = event
            self.save()

    async def after(self, response):
        event = self.requests[id(response.request)]
        event.update(
            response_headers_ms=round((time.monotonic() - self.started) * 1000, 3),
            upstream_status=response.status_code,
        )
        self.save()


def assess_result(result):
    """Keep transport completion, answer correctness and requested output bounds separate."""
    usage = result.get("usage")
    output = usage.get("completion_tokens") if isinstance(usage, dict) else None
    result["output_cap_respected"] = (
        0 <= output <= result["output_cap"] if type(output) is int else None
    )
    transport_ok = (
        result.get("http_status") == 200
        and not result.get("error_code")
        and not result.get("client_error_type")
    )
    if result["mode"] == "cancel":
        result["acceptance_passed"] = bool(
            transport_ok
            and result.get("closed_after_first_text", False)
            and result["output_cap_respected"] is not False
        )
    else:
        content_ok = (
            result.get("finish_reason") == "length"
            if result["mode"] == "limit"
            else result.get("finish_reason") == "stop" and result.get("correct_response", False)
        )
        result["acceptance_passed"] = bool(
            transport_ok
            and result.get("done")
            and content_ok
            and result["output_cap_respected"] is True
        )


def connections_from(directory, openai_key):
    server = load_config(directory / "server.json", ServerConfig)
    registry = load_config(directory / "connections.json", ConnectionsConfig)
    configured = {c.connection_id: c for c in registry.connections}
    selected = [
        (
            "openai-live",
            ConnectionConfig(
                connection_id="openai-live",
                name="OpenAI live acceptance",
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key=openai_key,
                allowed_models=["gpt-6-luna"],
                default_model="gpt-6-luna",
            ),
        ),
        ("deepseek-live", configured["deepseek-personal"]),
        ("mimo-live", legacy_connection(server)),
        ("relay-live", configured["relay-personal"]),
    ]
    result = []
    for identity, connection in selected:
        if (
            connection.resource_type != "api"
            or not connection.enabled
            or not connection.default_model
        ):
            raise ValueError("Only enabled, configured, API-key connections are accepted")
        result.append(
            connection.model_copy(
                update={
                    "connection_id": identity,
                    "allowed_models": [connection.default_model],
                    "timeout_seconds": 35.0,
                }
            )
        )
    return result


async def run(args, key):
    source = config_directory(args.config_dir)
    paths = [source / "server.json", source / "connections.json", source / "client.json"]
    original = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    connections = connections_from(source, key)
    secrets = [c.api_key.get_secret_value() for c in connections]
    guard = LiveGuard(args.work_dir, connections)
    report = {
        "version": __version__,
        "started_at": utc_now(),
        "scope": "real localhost HTTP and four real API upstreams; temporary profiles and ledger",
        "maximum_real_requests": MAX_REQUESTS,
        "maximum_per_connection": PER_CONNECTION,
        "generation_retries": 0,
        "credential_storage": "memory only",
        "models": {c.connection_id: c.default_model for c in connections},
        "calls": [],
        "phases": [],
        "capacity_samples": [],
        "upstream_timeout_seconds": 35,
        "gateway_deadline_seconds": 45,
    }

    def save():
        text = json.dumps(report, ensure_ascii=False)
        if any(secret in text for secret in secrets):
            raise ValueError("Credential detected in acceptance report")
        _write_private(args.work_dir / "report.json", report)

    # Explicit temporary data paths prevent all writes to the user's existing runtime.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("SELARK_")}
    with (
        patch.dict(os.environ, environment, clear=True),
        tempfile.TemporaryDirectory(prefix="selark-four-api-live-") as tmp,
    ):
        directory = Path(tmp)
        initialize(directory, "unused-live-placeholder")
        with Runtime(directory) as runtime:
            runtime.connection_settings = lambda: connections
            config = runtime.settings()
            config.limits = config.limits.model_copy(
                update={
                    "max_concurrent_requests": 4,
                    "max_queued_requests": 8,
                    "queue_timeout_seconds": 40.0,
                    "request_timeout_seconds": 45.0,
                    "connect_timeout_seconds": 15.0,
                }
            )
            app = create_app(config, runtime=runtime)
            tokens = {
                (c.connection_id, i): runtime.store.create_token(
                    c.connection_id + "-app-" + str(i),
                    policy={
                        "allowed_connections": [c.connection_id],
                        "allowed_models": [c.connection_id + "::" + c.default_model],
                        "max_output_tokens": 256 if c.provider == "openai" else 64,
                    },
                )
                for c in connections
                for i in range(2)
            }
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            healthy = {c.connection_id: True for c in connections}
            try:
                async with serve(app, runtime, sock), AsyncExitStack() as stack:
                    app.state.http.event_hooks["request"].append(guard.before)
                    app.state.http.event_hooks["response"].append(guard.after)
                    clients = {
                        identity: await stack.enter_async_context(
                            httpx.AsyncClient(
                                base_url=url,
                                trust_env=False,
                                timeout=50,
                                limits=httpx.Limits(max_connections=4, keepalive_expiry=0.5),
                                headers={"Authorization": "Bearer " + token["token"]},
                            )
                        )
                        for identity, token in tokens.items()
                    }
                    monitor = await stack.enter_async_context(
                        httpx.AsyncClient(
                            base_url=url,
                            trust_env=False,
                            timeout=5,
                        )
                    )

                    async def health():
                        response = await monitor.get("/health")
                        response.raise_for_status()
                        return response.json()

                    async def idle():
                        h = await health()
                        return h["capacity"]["admitted"] == h["storage"]["pending_records"] == 0

                    async def call(connection, phase, index=0, mode="normal", stream=False):
                        identity = connection.connection_id
                        expected = f"VERIFY_{identity}_{phase}_{index}"
                        prompt = "Reply exactly with: " + expected
                        cap = 256 if connection.provider == "openai" else 64
                        if mode in {"limit", "cancel"}:
                            prompt = "Print the integers from 1 to 1000 separated by spaces."
                        if mode == "limit":
                            cap = 8
                        result = {
                            "connection": identity,
                            "phase": phase,
                            "mode": mode,
                            "stream": stream,
                            "caller_index": index,
                            "output_cap": cap,
                        }
                        report["calls"].append(result)
                        text, usage, done, finish = "", None, False, None
                        started = time.monotonic()
                        try:
                            async with clients[identity, index].stream(
                                "POST",
                                "/v1/chat/completions",
                                json={
                                    "connection_id": identity,
                                    "model": connection.default_model,
                                    "messages": [{"role": "user", "content": prompt}],
                                    "max_tokens": cap,
                                    "stream": stream,
                                    "session_id": "shared-live-session",
                                },
                            ) as response:
                                result.update(
                                    http_status=response.status_code,
                                    request_id=response.headers.get("x-request-id"),
                                )
                                if response.status_code != 200:
                                    data = json.loads(await response.aread())
                                    result["error_code"] = data.get("error", {}).get("code")
                                elif stream:
                                    async for line in response.aiter_lines():
                                        if not line.startswith("data:"):
                                            continue
                                        raw = line[5:].strip()
                                        if raw == "[DONE]":
                                            done = True
                                            break
                                        data = json.loads(raw)
                                        if "error" in data:
                                            result["error_code"] = data["error"].get("code")
                                            break
                                        usage = data.get("usage") or usage
                                        for choice in data.get("choices", []):
                                            text += choice.get("delta", {}).get("content") or ""
                                            finish = choice.get("finish_reason") or finish
                                        if mode == "cancel" and text:
                                            result["closed_after_first_text"] = True
                                            break
                                else:
                                    data = json.loads(await response.aread())
                                    text = data["choices"][0]["message"]["content"]
                                    finish = data["choices"][0]["finish_reason"]
                                    usage, done = data.get("usage"), True
                        except Exception as exc:
                            result["client_error_type"] = type(exc).__name__
                        result.update(
                            duration_ms=round((time.monotonic() - started) * 1000, 2),
                            done=done,
                            finish_reason=finish,
                            usage=usage,
                            text_characters=len(text),
                            correct_response=text.strip() == expected,
                        )
                        assess_result(result)
                        guard.observe_result(result)
                        if result.get("error_code") or result.get("client_error_type"):
                            healthy[identity] = False
                        save()
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        return result

                    async def phase(name, specifications):
                        pending = list(specifications)
                        phase_result = {"phase": name, "requested_clients": len(pending)}
                        report["phases"].append(phase_result)
                        if (args.work_dir / "STOP").exists():
                            phase_result.update(
                                completed_clients=0, skipped_reason="acceptance_stopped"
                            )
                            save()
                            return
                        tasks = [asyncio.create_task(call(*spec)) for spec in pending]
                        try:
                            while any(not task.done() for task in tasks):
                                h = await health()
                                report["capacity_samples"].append({"phase": name, **h["capacity"]})
                                assert h["capacity"]["active"] <= 4 and h["capacity"]["queued"] <= 8
                                await asyncio.sleep(0.05)
                        finally:
                            results = await asyncio.gather(*tasks, return_exceptions=True)
                        assert not any(isinstance(r, BaseException) for r in results)
                        await wait_for(idle)
                        phase_result["completed_clients"] = len(results)
                        save()

                    for connection in connections:
                        await phase(
                            "single-" + connection.connection_id,
                            [
                                (connection, "single", 0, "normal", False),
                                (connection, "single", 1, "normal", True),
                            ],
                        )
                    await phase(
                        "mixed-eight",
                        [
                            (c, "mixed", index, "normal", bool(index))
                            for index in range(2)
                            for c in connections
                            if healthy[c.connection_id]
                        ],
                    )
                    for mode in ("limit", "cancel", "recovery"):
                        await phase(
                            mode,
                            [
                                (
                                    c,
                                    mode,
                                    0,
                                    mode if mode != "recovery" else "normal",
                                    mode == "cancel",
                                )
                                for c in connections
                                if healthy[c.connection_id]
                            ],
                        )
                    report["final_health"] = await health()
                    report["records"] = runtime.store.records({}, limit=100)
                    records = {r["request_id"]: r for r in report["records"]}
                    for result in report["calls"]:
                        row = records[result["request_id"]]
                        assert (
                            row["caller_id"]
                            == tokens[result["connection"], result["caller_index"]]["id"]
                        )
                        assert row["connection_id"] == result["connection"]
                        assert row["upstream_attempts"] in (0, 1)
                        if result["usage"] is not None:
                            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                assert row[field] == result["usage"].get(field)
                    report["ledger_checks_passed"] = True
                    report["acceptance_passed"] = len(report["calls"]) == MAX_REQUESTS and all(
                        result["acceptance_passed"] for result in report["calls"]
                    )
                report["recovery"] = runtime.recover_records()
                assert not any(report["recovery"].values())
                report["completed"] = True
            except Exception as exc:
                report.update(completed=False, failure_type=type(exc).__name__)
                raise
            finally:
                report["outgoing_attempts"] = dict(guard.counts)
                report["egress_events"] = guard.events
                report["stopped_reason"] = guard.stopped_reason
                report["source_configuration_unchanged"] = all(
                    hashlib.sha256(p.read_bytes()).hexdigest() == digest
                    for p, digest in original.items()
                )
                report["temporary_files_contain_provider_key"] = any(
                    secret.encode() in file.read_bytes()
                    for file in directory.rglob("*")
                    if file.is_file()
                    for secret in secrets
                )
                assert not report["temporary_files_contain_provider_key"]
                save()
    report["temporary_profiles_removed"] = True
    save()
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k
                in {
                    "completed",
                    "acceptance_passed",
                    "stopped_reason",
                    "outgoing_attempts",
                    "ledger_checks_passed",
                    "recovery",
                    "source_configuration_unchanged",
                    "temporary_profiles_removed",
                }
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Send at most 28 paid API requests")
    args = parser.parse_args()
    if not args.execute or os.environ.get("SELARK_DATA_DIR"):
        parser.error("Use --execute and unset SELARK_DATA_DIR for this isolated acceptance")
    args.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    key = os.environ.pop("OPENAI_API_KEY", None) or getpass.getpass("OpenAI API key (hidden): ")
    report = asyncio.run(run(args, key))
    raise SystemExit(0 if report["acceptance_passed"] else 1)
