"""Opt-in MiMo-only acceptance with a durable, non-reusable spending reservation.

All prompts are synthetic. Original profiles and services are read-only. Pricing is
the official CNY MiMo V2.6 Flash tariff checked on 2026-10-30; verify before re-use.
Local reservations are conservative safeguards, not provider invoices or a billing
source selector. The operator must verify sufficient unexpired gift credit first.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from application_scenarios import SCENARIOS, evaluate, make

from selark import __version__
from selark.app import create_app
from selark.backups import drill_backup, verify_backup
from selark.config import ServerConfig, _write_private, config_directory, initialize, load_config
from selark.governance import usage_cost
from selark.logs import configure_logging
from selark.persistence import sync_directory
from selark.providers.mimo import reported_usage
from selark.runtime import Runtime
from selark.storage import Store

MODEL = "mimo-v2.6-flash"
ORIGIN = "https://api.xiaomimimo.com/v1"
PRICE = {
    "connection_id": "mimo-default",
    "model": MODEL,
    "currency": "CNY",
    "input_per_million": "1",
    "cached_input_per_million": "0.02",
    "output_per_million": "2",
    "note": "Official CNY tariff verified 2026-10-30; acceptance estimate, not invoice",
}


def reserve_micros(payload):
    """No cache discount; UTF-8 allowance plus message overhead and full output cap."""
    if payload.get("model") != MODEL or payload.get("thinking") != {"type": "disabled"}:
        raise ValueError("Only the explicitly approved Flash text model is allowed")
    if set(payload) - {
        "model",
        "thinking",
        "messages",
        "stream",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "stop",
        "presence_penalty",
        "frequency_penalty",
    }:
        raise ValueError("Unapproved upstream feature")
    cap = payload.get("max_completion_tokens")
    if type(cap) is not int or not 1 <= cap <= 8192:
        raise ValueError("A bounded output limit is mandatory")
    body = json.dumps(payload["messages"], ensure_ascii=False).encode()
    if len(body) > 500000:
        raise ValueError("Acceptance input is too large")
    input_bound = 2 * len(body) + 1024 + 64 * len(payload["messages"])
    return input_bound + 2 * cap


class CreditTransport(httpx.AsyncBaseTransport):
    """The sole egress point. Persist the whole reservation BEFORE attempting HTTP."""

    def __init__(self, root, inner=None):
        self.root = Path(root)
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.lock = asyncio.Lock()
        self.next_start = 0.0

    async def handle_async_request(self, request):
        if request.method != "POST" or str(request.url) != ORIGIN + "/chat/completions":
            raise httpx.ConnectError("Unapproved upstream destination; not sent")
        payload = json.loads(request.content)
        reserve = reserve_micros(payload)
        async with self.lock:
            await asyncio.sleep(max(0, self.next_start - time.monotonic()))
            budget = json.loads((self.root / "budget.json").read_text())
            if (
                (self.root / "STOP").exists()
                or time.time() >= budget["deadline"]
                or budget["attempts"] >= budget["max_attempts"]
                or budget["reserved_micros"] + reserve > budget["maximum_micros"]
            ):
                raise httpx.ConnectError("Acceptance budget or deadline reached; not sent")
            budget["attempts"] += 1
            budget["reserved_micros"] += reserve
            budget["reservations"].append(
                {
                    "sequence": budget["attempts"],
                    "at": datetime.now(UTC).isoformat(),
                    "request_sha256": hashlib.sha256(request.content).hexdigest(),
                    "reserved_micros": reserve,
                    "max_completion_tokens": payload["max_completion_tokens"],
                }
            )
            _write_private(self.root / "budget.json", budget)
            sync_directory(self.root)
            self.next_start = time.monotonic() + 1.25  # <= 48 starts/minute
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


def serve(root):
    import uvicorn

    if os.environ.get("SELARK_DATA_DIR"):
        raise ValueError("Unset SELARK_DATA_DIR before isolated acceptance")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with Runtime(root / "profile") as runtime, runtime.service():
        settings = runtime.settings()
        handler = configure_logging(runtime.data_dir, settings.logging)
        try:
            uvicorn.run(
                create_app(settings, runtime=runtime, transport=CreditTransport(root)),
                host="127.0.0.1",
                port=settings.port,
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=20,
            )
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()


def fingerprint_originals(directory):
    from selark.config import data_directory

    paths = sorted(directory.rglob("*.json"))
    paths += sorted(data_directory(directory).glob("records.sqlite3*"))
    result = {}
    for path in paths:
        if path.name.endswith("-shm"):
            continue
        stat = path.stat()
        result[str(path)] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.st_uid,
            stat.st_gid,
            stat.st_mode & 0o777,
        )
    return result


class Acceptance:
    def __init__(self, args):
        if os.environ.get("SELARK_DATA_DIR"):
            raise ValueError("Unset SELARK_DATA_DIR before isolated acceptance")
        self.args = args
        self.root = args.work_dir.resolve()
        self.profile = self.root / "profile"
        self.process = self.log = None
        self.report = {
            "version": __version__,
            "started_at": datetime.now(UTC).isoformat(),
            "model": MODEL,
            "price": PRICE,
            "price_source": "https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go",
            "gift_credit_reported_cny": str(args.verified_gift_cny),
            "maximum_reservation_cny": str(args.max_reserve_cny),
            "no_paid_balance_authorized": True,
            "automatic_generation_retries": 0,
            "original_service_modified": False,
            "oauth_operations": 0,
            "calls": [],
            "checks": [],
            "phases": [],
        }
        self.tokens = {}
        self.failed = False

    def save(self):
        self.report["budget"] = json.loads((self.root / "budget.json").read_text())
        _write_private(self.root / "report.json", self.report)

    def initialize(self):
        if self.root.exists():
            raise ValueError("Use a new private work directory; never reset a consumed budget")
        if not 0 < self.args.max_reserve_cny <= self.args.verified_gift_cny * Decimal("0.75"):
            raise ValueError("Leave at least 25% of the verified gift credit untouched")
        self.root.mkdir(mode=0o700, parents=True)
        self.original = config_directory()
        self.before = fingerprint_originals(self.original)
        source = load_config(self.original / "server.json", ServerConfig)
        if source.base_url != ORIGIN or MODEL not in source.allowed_models:
            raise ValueError("Existing official MiMo Flash connection required")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        initialize(self.profile, source.api_key.get_secret_value(), models=[MODEL], port=port)
        document = json.loads((self.profile / "server.json").read_text())
        document["limits"].update(
            max_concurrent_requests=4,
            max_queued_requests=16,
            queue_timeout_seconds=30.0,
            request_timeout_seconds=180.0,
        )
        _write_private(self.profile / "server.json", document)
        with Runtime(self.profile) as runtime:
            runtime.edit_resource("price", ("mimo-default", MODEL), PRICE)
            runtime.edit_resource(
                "alias", "mimo-live", {"connection_id": "mimo-default", "model": MODEL}
            )
            for name in ("primary", "secondary"):
                caller = runtime.store.create_token(
                    name,
                    policy={
                        "allowed_connections": ["mimo-default"],
                        "allowed_models": [f"mimo-default::{MODEL}"],
                    },
                )
                self.tokens[name] = caller["token"]
            restricted = runtime.store.create_token("denied", policy={"allowed_connections": []})
            self.tokens["denied"] = restricted["token"]
        self.url = f"http://127.0.0.1:{port}"
        _write_private(
            self.root / "budget.json",
            {
                "maximum_micros": int(self.args.max_reserve_cny * 1000000),
                "reserved_micros": 0,
                "attempts": 0,
                "max_attempts": 1000,
                "deadline": time.time() + 7200,
                "reservations": [],
            },
        )
        self.save()

    async def start(self):
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("SELARK_") and k != "PYTHONPATH"
        }
        self.log = (self.root / "service.log").open("a")
        self.process = subprocess.Popen(
            [self.args.python, str(Path(__file__).resolve()), "--serve", str(self.root)],
            cwd=self.root,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(200):
            if self.process.poll() is not None:
                raise RuntimeError("Temporary service failed; inspect its private log")
            try:
                response = await self.http.get(self.url + "/health", timeout=1)
                if (
                    response.status_code == 200
                    and response.json().get("version") == __version__
                    and response.json().get("pid") == self.process.pid
                ):
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        raise RuntimeError("Temporary service startup timed out")

    async def stop(self, *, crash=False):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL if crash else signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, 25)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 10)
        if self.log:
            self.log.close()
        self.process = self.log = None

    def records(self):
        with contextlib.closing(
            Store(self.profile / "data/records.sqlite3", readonly=True)
        ) as store:
            return store.records({}, limit=10000)

    def check(self, name, passed, **details):
        self.report["checks"].append({"check": name, "passed": bool(passed), **details})
        self.save()
        if not passed:
            raise AssertionError(name)

    async def call(
        self,
        label,
        prompt=None,
        *,
        messages=None,
        stream=False,
        cap=256,
        caller="primary",
        options=None,
        expectation=None,
        disconnect=False,
    ):
        if self.failed or (self.root / "STOP").exists():
            return None
        payload = {
            "connection_id": "mimo-default",
            "model": MODEL,
            "session_id": label[:128],
            "messages": messages or [{"role": "user", "content": prompt}],
            "stream": stream,
            "max_tokens": cap,
            **(options or {}),
        }
        identity = None
        content, final_usage, finish = "", None, None
        completed = False
        start = time.monotonic()
        item = {
            "case": label,
            "stream": stream,
            "max_output_tokens": cap,
            "caller": caller,
            "expected_disconnect": disconnect,
        }
        try:
            async with self.http.stream(
                "POST",
                self.url + "/v1/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + self.tokens[caller]},
                timeout=220,
            ) as response:
                identity = response.headers.get("x-request-id")
                item["http_status"] = response.status_code
                item["connection"] = response.headers.get("x-selark-connection")
                if response.status_code != 200:
                    error = json.loads(await response.aread())
                    item["error_code"] = error.get("error", {}).get("code")
                elif not stream:
                    data = json.loads(await response.aread())
                    content = data["choices"][0]["message"].get("content") or ""
                    final_usage = reported_usage(data)
                    finish = data["choices"][0].get("finish_reason")
                    completed = True
                else:
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            completed = True
                            break
                        data = json.loads(raw)
                        if "error" in data:
                            item["error_code"] = data["error"].get("code")
                            break
                        if data.get("usage") is not None:
                            final_usage = reported_usage(data)
                        for choice in data.get("choices", []):
                            text = choice.get("delta", {}).get("content") or ""
                            if text and "first_text_ms" not in item:
                                item["first_text_ms"] = round((time.monotonic() - start) * 1000, 2)
                            content += text
                            finish = choice.get("finish_reason") or finish
                        if disconnect and content:
                            break
            row = None
            for _ in range(200):
                row = next((r for r in self.records() if r["request_id"] == identity), None)
                if row and row["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            item.update(
                request_id=identity,
                duration_ms=round((time.monotonic() - start) * 1000, 2),
                content=content,
                finish_reason=finish,
                received_done=completed,
                response_usage=final_usage,
                record=row,
            )
            if disconnect:
                item["transport_passed"] = bool(content and row and row["upstream_attempts"] == 1)
            else:
                item["transport_passed"] = bool(
                    completed
                    and row
                    and row["status"] == "success"
                    and row["usage_known"]
                    and row["reported_usage"] == final_usage
                    and row["connection_id"] == "mimo-default"
                    and row["model"] == MODEL
                    and row["upstream_attempts"] == 1
                    and row["completion_tokens"] <= cap
                    and row["resources"]["estimated_cost"] == usage_cost(PRICE, final_usage, "mimo")
                )
                if expectation:
                    try:
                        item["content_passed"] = bool(expectation(content, finish))
                    except (ValueError, TypeError, KeyError, IndexError):
                        item["content_passed"] = False
            if not item["transport_passed"]:
                self.failed = True
                (self.root / "STOP").touch(mode=0o600)
        except Exception as exc:
            item.update(transport_passed=False, exception_type=type(exc).__name__)
            self.failed = True
            (self.root / "STOP").touch(mode=0o600)
        self.report["calls"].append(item)
        self.save()
        print(
            json.dumps(
                {
                    k: item.get(k)
                    for k in (
                        "case",
                        "http_status",
                        "transport_passed",
                        "content_passed",
                        "duration_ms",
                        "error_code",
                    )
                }
            ),
            flush=True,
        )
        return item

    async def local_checks(self):
        before = json.loads((self.root / "budget.json").read_text())["attempts"]
        for label, payload, caller, status in [
            ("empty_messages", {"messages": []}, "primary", 400),
            (
                "wrong_type",
                {"messages": [{"role": "user", "content": "x"}], "stream": "true"},
                "primary",
                400,
            ),
            (
                "tools_rejected",
                {"messages": [{"role": "user", "content": "x"}], "tools": []},
                "primary",
                400,
            ),
            (
                "unknown_model",
                {"messages": [{"role": "user", "content": "x"}], "model": "not-allowed"},
                "primary",
                403,
            ),
            ("denied_caller", {"messages": [{"role": "user", "content": "x"}]}, "denied", 403),
        ]:
            response = await self.http.post(
                self.url + "/v1/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + self.tokens[caller]},
            )
            self.check(label, response.status_code == status, status=response.status_code)
        response = await self.http.get(
            self.url + "/v1/models", headers={"Authorization": "Bearer invalid"}
        )
        self.check("wrong_auth", response.status_code == 401)
        self.check(
            "local_rejections_do_not_generate",
            json.loads((self.root / "budget.json").read_text())["attempts"] == before,
        )

    async def smoke(self):
        await self.local_checks()
        for i in range(4):
            await self.call(
                f"smoke-{i}",
                "Reply with exactly SELARK_OK.",
                stream=bool(i % 2),
                caller="primary" if i < 2 else "secondary",
                cap=32,
                expectation=lambda text, _: text.strip() == "SELARK_OK",
            )
        for alias in (False, True):
            await self.call(
                f"explicit-alias-{alias}",
                "Return only the number 42.",
                cap=16,
                options={"model": "@mimo-live"} if alias else {},
                expectation=lambda text, _: text.strip() == "42",
            )

    async def tasks(self):
        for scenario in SCENARIOS:
            histories = {v: [] for v in ("a", "b")}
            for turn in (1, 2, 3):
                calls = []
                for variant in ("a", "b"):
                    case = make(scenario, variant, turn)
                    histories[variant].append({"role": "user", "content": case["prompt"]})
                    calls.append(
                        self.call(
                            f"task-{scenario}-{variant}-{turn}",
                            messages=histories[variant],
                            stream=turn != 1,
                            cap=4096,
                            caller="primary" if variant == "a" else "secondary",
                        )
                    )
                results = await asyncio.gather(*calls)
                for variant, item in zip(("a", "b"), results, strict=True):
                    if item is None or not item["transport_passed"]:
                        return
                    case = make(scenario, variant, turn)
                    item["assessment"] = evaluate(item["content"], case)
                    histories[variant].append({"role": "assistant", "content": item["content"]})
                self.save()

    async def long_context(self):
        for length in (1024, 8192, 32768, 65536):
            for position in (0, 0.5, 1):
                for streaming in (False, True):
                    label = f"long-{length}-{position}-{streaming}"
                    marker = hashlib.sha256(label.encode()).hexdigest()[:12]
                    lines = [
                        f"row={n:05} color=grey count={(n * 7) % 97:02}\n"
                        for n in range(length // 32)
                    ]
                    lines.insert(int(len(lines) * position), f"TARGET_ACCESS_CODE={marker}\n")
                    prompt = (
                        "Synthetic data. Extract TARGET_ACCESS_CODE. "
                        "Reply only with its exact value.\n" + "".join(lines)
                    )
                    await self.call(
                        label,
                        prompt,
                        cap=64,
                        stream=streaming,
                        expectation=lambda text, _, marker=marker: text.strip() == marker,
                    )
                    if self.failed:
                        return

    async def matrix(self, count, *, start=0):
        async def one(i):
            mode = i % 6
            a, b = (i * 17) % 971, (i * 11) % 863
            marker = f"Q{i:04}-中文-🌱"
            options = {"temperature": (i % 4) * 0.3}
            expected = None
            if mode == 0:
                prompt = f"Compute {a}+{b}. Return only the integer."

                def expected(text, _):
                    return text.strip() == str(a + b)
            elif mode == 1:
                prompt = (
                    "Return exactly this JSON with no markdown: "
                    f'{{"id":{i},"ok":true,"missing":null}}'
                )

                def expected(text, _):
                    return json.loads(text) == {"id": i, "ok": True, "missing": None}
            elif mode == 2:
                prompt = f"Repeat this exact text once, without quotes: {marker}"

                def expected(text, _):
                    return text.strip() == marker
            elif mode == 3:
                prompt = (
                    f"Extract the order id as JSON only. Text: Order #{10000 + i} is ready. "
                    'Format: {"order_id":123}'
                )

                def expected(text, _):
                    return json.loads(text) == {"order_id": 10000 + i}
            elif mode == 4:
                prompt = (
                    f"List these numbers in ascending order, comma separated, no spaces: {b},{a},-3"
                )

                def expected(text, _):
                    return text.strip() == ",".join(map(str, sorted([a, b, -3])))
            else:
                prompt = f"Return exactly: LEFT-{i}|STOP|RIGHT-{i}"
                options["stop"] = "|STOP|"

                def expected(text, _):
                    return text.strip() == f"LEFT-{i}"

            messages = [{"role": "user", "content": prompt}]
            if i % 9 == 0:
                messages[0]["content"] = [{"type": "text", "text": prompt}]
            if i % 11 == 0:
                messages.insert(
                    0, {"role": "system", "content": "Follow the output format exactly."}
                )
            return await self.call(
                f"matrix-{i:04}",
                messages=messages,
                cap=128,
                stream=bool(i % 2),
                caller="secondary" if i % 3 == 0 else "primary",
                options=options,
                expectation=expected,
            )

        for first in range(start, start + count, 4):
            await asyncio.gather(*(one(i) for i in range(first, min(first + 4, start + count))))
            if self.failed:
                return

    async def boundaries(self):
        for cap in (1, 2, 8, 32):
            for stream in (False, True):
                await self.call(
                    f"output-cap-{cap}-{stream}",
                    "Count from 1 to 1000, separated by spaces.",
                    cap=cap,
                    stream=stream,
                    expectation=lambda _, finish: finish == "length",
                )
        for length in (2048, 8192, 32768):
            prefix = "Aster archive: blue shelves, 12 rooms, opened in 2021. " * (length // 56)
            for repetition in range(3):
                await self.call(
                    f"cache-{length}-{repetition}",
                    prefix + "\nHow many rooms? Reply only 12.",
                    cap=16,
                    stream=bool(repetition % 2),
                    expectation=lambda text, _: text.strip() == "12",
                )

    async def operations(self):
        before = self.records()
        attempts = json.loads((self.root / "budget.json").read_text())["attempts"]
        with Runtime(self.profile) as runtime:
            backup = runtime.backup("real-mimo-acceptance")
        self.check("backup_verified", verify_backup(backup)["ok"])
        drill = drill_backup(backup)
        self.check("isolated_recovery", drill["ok"] and drill["usage"]["requests"] == len(before))
        await self.stop()
        await self.start()
        self.check("graceful_restart_preserves_history", self.records() == before)
        await self.stop(crash=True)  # Idle owned process only: no generation is abandoned.
        await self.start()
        self.check("idle_crash_recovery_preserves_history", self.records() == before)
        self.check(
            "operations_generate_nothing",
            json.loads((self.root / "budget.json").read_text())["attempts"] == attempts,
        )

    async def run(self):
        self.initialize()
        async with httpx.AsyncClient(
            trust_env=False, timeout=220, limits=httpx.Limits(keepalive_expiry=1)
        ) as self.http:
            try:
                await self.start()
                for name, task in (
                    ("smoke", self.smoke),
                    ("tasks", self.tasks),
                    ("long_context", self.long_context),
                    ("boundaries", self.boundaries),
                    ("matrix", lambda: self.matrix(self.args.matrix_calls)),
                    ("operations", self.operations),
                ):
                    if self.failed:
                        break
                    print(json.dumps({"phase": name, "event": "start"}), flush=True)
                    await task()
                    self.report["phases"].append({"name": name, "completed": not self.failed})
                    self.save()
                if not self.failed:
                    # Deliberate uncertainty is last; all of its maximum output remains reserved.
                    await self.call(
                        "final-client-disconnect",
                        "Count from 1 to 100, separated by spaces.",
                        stream=True,
                        cap=128,
                        disconnect=True,
                    )
                    before = self.records()
                    await self.stop()
                    with Runtime(self.profile) as runtime:
                        recovery = runtime.recover_records()
                    self.check(
                        "disconnect_recovery_does_not_duplicate",
                        self.records() == before,
                        recovery=recovery,
                    )
            except Exception as exc:
                self.report["failure_type"] = type(exc).__name__
                self.failed = True
            finally:
                await self.stop()
                with contextlib.closing(
                    Store(self.profile / "data/records.sqlite3", readonly=True)
                ) as store:
                    self.report["summary"] = store.summary({})["summary"]
                    self.report["records"] = store.records({}, limit=10000)
                self.report["original_files_unchanged"] = (
                    fingerprint_originals(self.original) == self.before
                )
                self.report["finished_at"] = datetime.now(UTC).isoformat()
                self.report["transport_passed"] = not self.failed
                self.report["temporary_service_stopped"] = True
                self.save()
                print(
                    json.dumps(
                        {
                            "complete": not self.failed,
                            "calls": len(self.report["calls"]),
                            "summary": self.report["summary"],
                            "original_files_unchanged": self.report["original_files_unchanged"],
                        }
                    ),
                    flush=True,
                )
        return 1 if self.failed else 0


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--python", required=True, help="Installed Selark Python interpreter")
    parser.add_argument("--verified-gift-cny", type=Decimal, required=True)
    parser.add_argument("--max-reserve-cny", type=Decimal, required=True)
    parser.add_argument("--matrix-calls", type=int, default=420, choices=range(1, 801))
    return asyncio.run(Acceptance(parser.parse_args()).run())


if __name__ == "__main__":
    raise SystemExit(main())
