"""Opt-in broad MiMo acceptance. Synthetic inputs, memory-only key, durable CNY budget.

This is a finite application matrix, not a balance-draining loop. The reservation
is conservative and never refunded; provider billing and remaining balance are unknown.
"""

import argparse
import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import os
import socket
import tempfile
import time
import wave
from collections import Counter
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from application_scenarios import SCENARIOS, SYSTEM, make
from mimo_extended_cases import (
    ADD_TOOL,
    FLASH,
    MODELS,
    PRO,
    calculate_fixture,
    case,
    evaluate,
    focused_cases,
    static_phases,
    vision_cases,
)
from verify_openai_luna import normalize_usage, responses_final
from verify_resource_matrix import serve, wait_for

from selark.app import create_app
from selark.config import ServerConfig, _write_private, config_directory, initialize, load_config
from selark.connections import ConnectionConfig, ConnectionsConfig, legacy_connection
from selark.providers.mimo import retry_after_seconds
from selark.runtime import Runtime
from selark.storage import utc_now

ORIGIN = "https://api.xiaomimimo.com"
TTS, ASR = "mimo-v2.5-tts", "mimo-v2.5-asr"
MAX_ATTEMPTS, MAX_OUTPUT, MAX_MICROS = 400, 16384, 20_000_000
RATES = {FLASH: (1, 2), PRO: (3, 6)}
ENDPOINTS = {
    "gateway": "/v1/chat/completions",
    "chat": ORIGIN + "/v1/chat/completions",
    "responses": ORIGIN + "/v1/responses",
    "anthropic": ORIGIN + "/anthropic/v1/messages",
}


def budget_micros(value):
    amount = Decimal(str(value)) * 1_000_000
    if not amount.is_finite() or not 0 < amount <= MAX_MICROS or amount != int(amount):
        raise ValueError("Budget must be positive, at most CNY 20, with at most six decimals")
    return int(amount)


def stress_phases():
    """Finite text-only regression and sustained output load, within CNY 8 reservation."""
    phases = static_phases()
    for index, (name, concurrency, cases) in enumerate(phases):
        if name == "long-reference":
            # Keep 16/64/128 KiB; reserve budget for sustained delivery as well.
            phases[index] = (
                name,
                concurrency,
                [c for c in cases if len(json.dumps(c["payload"]).encode()) < 150_000],
            )
    templates = next(cases for name, _, cases in phases if name == "substantive-long-output")
    contexts = (
        "低内存笔记本，特别说明资源限制",
        "离线桌面应用，特别说明断电恢复",
        "多人共享电脑，特别说明应用权限隔离",
        "十万条记录，特别说明分页与检索边界",
        "磁盘空间不足，特别说明写入失败处理",
        "低速网络，特别说明流式取消与重连",
        "首次使用的新用户，特别说明错误提示与操作步骤",
        "升级迁移，特别说明兼容检查和回滚",
    )
    soak = []
    for i in range(96):
        source = templates[i % len(templates)]
        prompt = (
            f"虚构验收案例 {i + 1}：{contexts[(i // len(templates)) % len(contexts)]}。\n"
            + source["payload"]["messages"][0]["content"]
        )
        soak.append(
            case(
                f"sustained-output-{i}",
                prompt,
                model=source["model"],
                route="gateway",
                stream=i % 3 != 0,
                cap=8192,
                check="substantive",
                expected=source["expected"],
            )
        )
    phases.insert(-2, ("sustained-gateway-output", 8, soak))
    return phases


def reservation(body):
    model = body.get("model")
    cap = body.get("max_completion_tokens", body.get("max_output_tokens", body.get("max_tokens")))
    if model not in (*MODELS, TTS, ASR):
        raise ValueError("Unapproved model")
    if model not in (TTS, ASR) and (type(cap) is not int or not 1 <= cap <= MAX_OUTPUT):
        raise ValueError("Explicit bounded output required")
    if any(k in body for k in ("background", "previous_response_id", "store")):
        raise ValueError("Server-side state is outside this acceptance")
    content = copy.deepcopy(body)
    media_allowance = 0

    def strip_media(value):
        nonlocal media_allowance
        if isinstance(value, dict):
            if value.get("type") == "image_url":
                url = value["image_url"]["url"]
                if not url.startswith("data:image/png;base64,"):
                    raise ValueError("Only inline synthetic PNG fixtures")
                image = base64.b64decode(url.split(",", 1)[1], validate=True)
                if image[:8] != b"\x89PNG\r\n\x1a\n" or len(image) > 100000:
                    raise ValueError("Unexpected image")
                import struct

                width, height = struct.unpack("!II", image[16:24])
                if width > 512 or height > 512:
                    raise ValueError("Image dimensions exceed fixture limit")
                media_allowance += 30000
                value["image_url"]["url"] = "[inline fixture]"
            elif value.get("type") == "input_audio":
                data = value["input_audio"]["data"]
                if data.startswith("data:"):
                    if not data.startswith("data:audio/wav;base64,"):
                        raise ValueError("Only generated WAV fixtures")
                    data = data.split(",", 1)[1]
                raw = base64.b64decode(data, validate=True)
                if len(raw) > 5_000_000:
                    raise ValueError("Audio fixture too large")
                with wave.open(io.BytesIO(raw)) as audio:
                    seconds = audio.getnframes() / audio.getframerate()
                if not 0 < seconds <= 30:
                    raise ValueError("Audio must be at most 30 seconds")
                media_allowance += math.ceil(seconds * 2000)
                value["input_audio"]["data"] = "[inline fixture]"
            elif value.get("type") in {"video_url", "file", "input_file"}:
                raise ValueError("Unbounded media is outside this acceptance")
            else:
                for item in value.values():
                    strip_media(item)
        elif isinstance(value, list):
            for item in value:
                strip_media(item)

    strip_media(content)
    encoded = json.dumps(content, ensure_ascii=False).encode()
    if len(encoded) > 1_100_000:
        raise ValueError("Input too large")
    search = 0
    for tool in body.get("tools", []):
        if tool.get("type") == "web_search":
            search += 1_000_000  # Separate search service, conservatively reserve CNY 1.
        elif tool.get("type") != "function" or tool.get("function", tool).get("name") not in {
            "add_numbers",
            "calculate_fixture",
        }:
            raise ValueError("Unapproved tool")
    if search > 1_000_000:
        raise ValueError("Only one search declaration per request")
    if model == TTS:
        if len(encoded) > 1500 or body.get("audio", {}).get("voice") not in (None, "mimo_default"):
            raise ValueError("Only short built-in-voice speech")
        return 100000
    if model == ASR:
        if not media_allowance:
            raise ValueError("ASR requires bounded generated audio")
        return 100000
    inputs = 2 * len(encoded) + 2048 + media_allowance
    return inputs * RATES[model][0] + cap * RATES[model][1] + search


class MiMoGuard:
    def __init__(self, directory, key, *, maximum=MAX_MICROS, attempts=MAX_ATTEMPTS):
        self.directory, self.key, self.maximum, self.max_attempts = (
            directory,
            key,
            maximum,
            attempts,
        )
        self.events, self.lookup = [], {}
        self.reserved, self.metadata_reads = 0, 0
        self.stop_reason, self.phase = None, "initialization"
        self.started = time.monotonic()
        self.pause_until, self.rate_failures, self.server_failures = 0, 0, 0
        self.lock = asyncio.Lock()
        self.path = directory / "budget.json"
        with self.path.open("x"):
            pass
        self.path.chmod(0o600)
        self.save()

    def save(self):
        _write_private(
            self.path,
            {
                "maximum_micros": self.maximum,
                "reserved_micros": self.reserved,
                "attempts": len(self.events),
                "max_attempts": self.max_attempts,
                "metadata_reads": self.metadata_reads,
                "stop_reason": self.stop_reason,
                "events": self.events,
            },
        )

    async def before(self, request):
        if request.headers.get("api-key") != self.key:
            raise httpx.ConnectError("Incorrect credential binding; not sent", request=request)
        if request.method == "GET" and str(request.url) == ORIGIN + "/v1/models":
            if self.metadata_reads:
                raise httpx.ConnectError("Metadata already read", request=request)
            self.metadata_reads += 1
            self.save()
            return
        if request.method != "POST" or str(request.url) not in set(ENDPOINTS.values()) - {
            ENDPOINTS["gateway"]
        }:
            raise httpx.ConnectError("Unapproved endpoint; not sent", request=request)
        try:
            body = json.loads(request.content)
            reserve = reservation(body)
        except (ValueError, KeyError, TypeError) as exc:
            raise httpx.ConnectError(
                "Invalid acceptance payload; not sent", request=request
            ) from exc
        while True:
            async with self.lock:
                now = time.monotonic()
                if (self.directory / "STOP").exists():
                    self.stop_reason = "operator_stop"
                    self.save()
                if (
                    self.stop_reason
                    or (self.directory / "STOP").exists()
                    or now - self.started > 5400
                    or len(self.events) >= self.max_attempts
                ):
                    raise httpx.ConnectError("Acceptance stopped before send", request=request)
                if self.reserved + reserve > self.maximum:
                    self.stop_reason = "reservation_budget_exhausted"
                    self.save()
                    raise httpx.ConnectError("Budget exhausted before send", request=request)
                recent = [
                    e
                    for e in self.events
                    if e["model"] == body["model"]
                    and now - self.started - e["started_seconds"] < 60
                ]
                delay = max(0, self.pause_until - now)
                if len(recent) >= 80:
                    delay = max(delay, 60 - (now - self.started - recent[0]["started_seconds"]))
                if delay <= 0:
                    self.reserved += reserve
                    event = {
                        "sequence": len(self.events) + 1,
                        "phase": self.phase,
                        "model": body["model"],
                        "endpoint": request.url.path,
                        "direct": request.extensions.get("mimo_direct", False),
                        "started_seconds": now - self.started,
                        "reserved_micros": reserve,
                        "body_bytes": len(request.content),
                        "payload_sha256": hashlib.sha256(request.content).hexdigest(),
                    }
                    self.events.append(event)
                    self.lookup[id(request)] = event
                    self.save()  # Durable BEFORE transmission. No refunds on any outcome.
                    return
            await asyncio.sleep(min(1, delay))

    async def after(self, response):
        if response.request.method == "GET":
            return
        event = self.lookup[id(response.request)]
        event.update(
            http_status=response.status_code, headers_seconds=time.monotonic() - self.started
        )
        if response.status_code in (401, 402, 403):
            self.stop_reason = "authentication_access_or_balance"
        elif response.status_code == 429:
            self.rate_failures += 1
            wait = retry_after_seconds(response.headers.get("retry-after")) or 30
            if self.rate_failures >= 3 or wait > 120:
                self.stop_reason = "repeated_rate_limit"
            self.pause_until = time.monotonic() + wait
        if response.status_code >= 500:
            self.server_failures += 1
            self.pause_until = max(self.pause_until, time.monotonic() + 5)
            if self.server_failures >= 3:
                self.stop_reason = "repeated_server_failure"
        else:
            self.server_failures = 0
        self.save()


class Collector:
    def __init__(self, route):
        self.route, self.text, self.reasoning = route, "", ""
        self.functions, self.usage, self.finish = {}, None, None
        self.done, self.error = False, None
        self.output, self.model, self.annotations, self.web_usage = [], None, [], None
        self.audio = ""

    def feed(self, data, *, stream):
        if data.get("error"):
            self.error = data["error"]
            return
        self.model = data.get("model") or self.model
        if self.route == "responses":
            event = data.get("type")
            if not stream or event in {"response.completed", "response.incomplete"}:
                final = data["response"] if stream else data
                self.text, functions, self.usage, self.finish = responses_final(final)
                self.functions = dict(enumerate(functions))
                self.output = final.get("output", [])
                self.model = final.get("model")
                self.done = final.get("status") in {"completed", "incomplete"}
            elif event == "response.output_text.delta":
                self.text += data.get("delta", "")
            elif event in {
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
            }:
                self.reasoning += data.get("delta", "")
            elif event in {"response.failed", "error"}:
                self.error = data
            return
        if self.route == "anthropic":
            event = data.get("type")
            if not stream or event == "message_start":
                message = data["message"] if stream else data
                self.model = message.get("model")
                self.usage = message.get("usage") or self.usage
                for part in message.get("content", []):
                    if part["type"] == "text":
                        self.text += part.get("text", "")
                    elif part["type"] == "thinking":
                        self.reasoning += part.get("thinking", "")
                self.finish = message.get("stop_reason")
                self.done = not stream
            elif event == "content_block_delta":
                delta = data.get("delta", {})
                self.text += delta.get("text", "")
                self.reasoning += delta.get("thinking", "")
            elif event == "message_delta":
                self.finish = data.get("delta", {}).get("stop_reason")
                self.usage = (self.usage or {}) | (data.get("usage") or {})
            elif event == "message_stop":
                self.done = True
            return
        self.usage = data.get("usage") or self.usage
        self.web_usage = (
            data.get("web_search_usage")
            or (data.get("usage") or {}).get("web_search_usage")
            or self.web_usage
        )
        for choice in data.get("choices") or []:
            part = choice.get("delta" if stream else "message") or {}
            self.text += part.get("content") or ""
            self.reasoning += part.get("reasoning_content") or ""
            self.audio += (part.get("audio") or {}).get("data") or ""
            self.annotations.extend(part.get("annotations") or [])
            self.finish = choice.get("finish_reason") or self.finish
            for i, f in enumerate(part.get("tool_calls") or []):
                current = self.functions.setdefault(
                    f.get("index", i), {"id": None, "name": "", "arguments": ""}
                )
                current["id"] = f.get("id") or current["id"]
                function = f.get("function") or {}
                current["name"] += function.get("name") or ""
                current["arguments"] += function.get("arguments") or ""
        if not stream:
            self.done = True

    def result(self):
        usage = self.usage
        if self.route == "anthropic" and usage:
            inputs = usage.get("input_tokens", 0)
            cache = usage.get("cache_read_input_tokens", 0)
            writes = usage.get("cache_creation_input_tokens", 0)
            output = usage.get("output_tokens", 0)
            usage = {
                "prompt_tokens": inputs + cache + writes,
                "completion_tokens": output,
                "total_tokens": inputs + cache + writes + output,
                "prompt_tokens_details": {"cached_tokens": cache},
            }
        return {
            "text": self.text,
            "reasoning_content": self.reasoning,
            "reasoning_characters": len(self.reasoning),
            "usage": normalize_usage(usage),
            "finish_reason": self.finish,
            "done": self.done,
            "function_calls": list(self.functions.values()),
            "response_output": self.output,
            "returned_model": self.model,
            "annotations": self.annotations,
            "web_search_usage": self.web_usage,
        }


class Runner:
    def __init__(self, root, guard):
        self.root, self.guard = root, guard
        self.report = {
            "started_at": utc_now(),
            "completed": False,
            "models": list(MODELS),
            "calls": [],
            "phases": [],
            "generation_retries": 0,
            "reservation_budget_cny": guard.maximum / 1_000_000,
            "balance_verified": False,
        }
        self.gateway_clients, self.direct = [], None
        self.health = None
        self.phase = "initialization"

    def save(self):
        raw = json.dumps(self.report, ensure_ascii=False).replace(self.guard.key, "[redacted-key]")
        _write_private(self.root / "report.json", json.loads(raw))

    async def call(self, c):
        index = len(self.report["calls"])
        r = {
            "name": c["name"],
            "phase": self.phase,
            "route": c["route"],
            "model": c["model"],
            "stream": c["stream"],
            "thinking": c["thinking"],
            "cap": c["cap"],
            "check": c["check"],
            "expected": c["expected"],
            "direct": c["route"] != "gateway",
            "caller_index": index % 8,
        }
        self.report["calls"].append(r)
        payload = c["payload"]
        _write_private(self.root / f"request-{index:03}.json", c)
        if self.guard.stop_reason:
            r.update(skipped=self.guard.stop_reason, passed=False)
            self.save()
            return r
        client = self.direct if r["direct"] else self.gateway_clients[index % 8]
        collector, started = Collector(c["route"]), time.monotonic()
        try:
            async with asyncio.timeout(1 if c.get("cancel") == "before-headers" else 360):
                async with client.stream(
                    "POST",
                    ENDPOINTS[c["route"]],
                    json=payload,
                    extensions={"mimo_direct": r["direct"]},
                ) as response:
                    r.update(
                        http_status=response.status_code,
                        request_id=response.headers.get("x-request-id"),
                        headers_ms=(time.monotonic() - started) * 1000,
                    )
                    if response.status_code != 200:
                        data = await response.aread()
                        try:
                            error = json.loads(data).get("error", {})
                            r.update(
                                error_code=error.get("code") or error.get("type"),
                                error_message=str(error.get("message", ""))[:1500],
                            )
                        except (ValueError, AttributeError):
                            r["error_code"] = "unstructured_http_error"
                    elif c["stream"]:
                        async for line in response.aiter_lines():
                            if c.get("read_delay"):
                                await asyncio.sleep(c["read_delay"])
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                collector.done = True
                                break
                            collector.feed(json.loads(data), stream=True)
                            if collector.error:
                                r["error_code"] = "upstream_stream_error"
                                break
                            if collector.text and "first_text_ms" not in r:
                                r["first_text_ms"] = (time.monotonic() - started) * 1000
                            if collector.reasoning and "first_reasoning_ms" not in r:
                                r["first_reasoning_ms"] = (time.monotonic() - started) * 1000
                            if collector.text and c.get("cancel") == "first-text":
                                r["intentionally_cancelled"] = True
                                break
                            if len(collector.text) + len(collector.reasoning) > 160000:
                                r["error_code"] = "response_size_limit"
                                break
                    else:
                        collector.feed(json.loads(await response.aread()), stream=False)
        except TimeoutError:
            if c.get("cancel") == "before-headers":
                r["intentionally_cancelled"] = True
            else:
                r["client_error_type"] = "TotalTimeout"
        except Exception as exc:
            r["client_error_type"] = type(exc).__name__
            if isinstance(exc, (TypeError, ValueError, KeyError, AttributeError)):
                r["parser_error_detail"] = str(exc)[:300]
                self.guard.stop_reason = "harness_parser_error"
        r.update(collector.result(), duration_ms=(time.monotonic() - started) * 1000)
        if collector.audio:
            try:
                audio = base64.b64decode(collector.audio, validate=True)
                path = self.root / f"audio-{index:03}.wav"
                path.write_bytes(audio)
                path.chmod(0o600)
                with wave.open(io.BytesIO(audio)) as wav:
                    r["audio_seconds"] = wav.getnframes() / wav.getframerate()
                r.update(audio_file=path.name, audio_bytes=len(audio))
            except Exception as exc:
                r["audio_error"] = type(exc).__name__
        evaluate(c, r)
        if r["cap_respected"] is False:
            self.guard.stop_reason = "reported_output_exceeded_cap"
        if r.get("error_code") in {"insufficient_quota", "insufficient_balance"}:
            self.guard.stop_reason = "insufficient_balance"
        self.guard.save()
        self.save()
        print(
            "CASE "
            + json.dumps(
                {
                    k: r.get(k)
                    for k in (
                        "name",
                        "http_status",
                        "passed",
                        "error_code",
                        "client_error_type",
                        "duration_ms",
                        "usage",
                    )
                }
            ),
            flush=True,
        )
        return r

    async def phase_cases(self, name, concurrency, cases):
        self.phase = self.guard.phase = name
        info = {
            "name": name,
            "clients": len(cases),
            "concurrency": concurrency,
            "peak_active": 0,
            "peak_queued": 0,
        }
        self.report["phases"].append(info)
        gate = asyncio.Semaphore(concurrency)

        async def work(c):
            async with gate:
                return await self.call(c)

        jobs = [asyncio.create_task(work(c)) for c in cases]
        while not all(job.done() for job in jobs):
            if self.health:
                health = await self.health()
                info["peak_active"] = max(info["peak_active"], health["capacity"]["active"])
                info["peak_queued"] = max(info["peak_queued"], health["capacity"]["queued"])
                assert health["capacity"]["active"] <= 8 and health["capacity"]["queued"] <= 8
            await asyncio.sleep(0.1)
        results = await asyncio.gather(*jobs)
        info.update(
            passed=sum(r.get("passed", False) for r in results),
            completed=sum(r.get("done", False) for r in results),
            errors=dict(
                Counter(
                    r.get("error_code") or r.get("client_error_type")
                    for r in results
                    if not r.get("passed")
                )
            ),
        )
        self.save()
        print("PHASE " + json.dumps(info), flush=True)
        return results

    async def conversations(self):
        self.phase = self.guard.phase = "application-conversations"
        gate = asyncio.Semaphore(4)

        async def conversation(model, scenario):
            async with gate:
                messages = [{"role": "system", "content": SYSTEM}]
                for turn in (1, 2, 3):
                    spec = make(scenario, "a", turn)
                    messages.append({"role": "user", "content": spec["prompt"]})
                    c = case(
                        f"application-{model}-{scenario}-{turn}",
                        model=model,
                        stream=turn % 2 == 0,
                        thinking=scenario == "coding",
                        cap=8192,
                        messages=copy.deepcopy(messages),
                        check="application",
                        expected=None,
                        application=spec,
                    )
                    r = await self.call(c)
                    r["application"] = {"scenario": scenario, "variant": "a", "turn": turn}
                    if not r.get("done"):
                        break
                    messages.append(
                        {
                            "role": "assistant",
                            "content": r["text"],
                            **(
                                {"reasoning_content": r["reasoning_content"]}
                                if r["reasoning_content"]
                                else {}
                            ),
                        }
                    )

        await asyncio.gather(*(conversation(m, s) for m in MODELS for s in SCENARIOS))

    async def tool_workflows(self):
        self.phase = self.guard.phase = "function-roundtrips"
        for model in MODELS:
            for thinking in (False, True):
                for route in ("chat", "responses"):
                    c = case(
                        f"tool-{model}-{thinking}-{route}",
                        "Use add_numbers for 17 + 24. "
                        "After receiving its result reply with only the integer sum.",
                        model=model,
                        route=route,
                        stream=True,
                        thinking=thinking,
                        cap=8192,
                        check="tool",
                        expected=[{"a": 17, "b": 24}],
                    )
                    c["payload"]["tools"] = [
                        copy.deepcopy(ADD_TOOL)
                        if route == "chat"
                        else {"type": "function", **copy.deepcopy(ADD_TOOL["function"])}
                    ]
                    c["payload"]["tool_choice"] = "auto"
                    r = await self.call(c)
                    if not r.get("passed"):
                        continue
                    f = r["function_calls"][0]
                    args = json.loads(f["arguments"])
                    if set(args) != {"a", "b"} or any(type(v) is not int for v in args.values()):
                        raise ValueError("Function arguments rejected")
                    answer = str(args["a"] + args["b"])
                    following = copy.deepcopy(c)
                    following.update(name=c["name"] + "-followup", check="exact", expected=answer)
                    if route == "chat":
                        following["payload"]["messages"] += [
                            {
                                "role": "assistant",
                                "content": r["text"],
                                "reasoning_content": r["reasoning_content"],
                                "tool_calls": [
                                    {
                                        "id": f["id"],
                                        "type": "function",
                                        "function": {
                                            "name": f["name"],
                                            "arguments": f["arguments"],
                                        },
                                    }
                                ],
                            },
                            {"role": "tool", "tool_call_id": f["id"], "content": answer},
                        ]
                    else:
                        following["payload"]["input"] += r["response_output"] + [
                            {"type": "function_call_output", "call_id": f["id"], "output": answer}
                        ]
                    await self.call(following)
        for model in MODELS:
            c = case(
                f"multiple-functions-{model}",
                "Call add_numbers exactly twice: a=2,b=3 and a=5,b=7. Return both calls, no text.",
                model=model,
                cap=4096,
                check="tool",
                expected=[{"a": 2, "b": 3}, {"a": 5, "b": 7}],
            )
            c["payload"]["tools"] = [ADD_TOOL]
            await self.call(c)

    async def audio_workflows(self):
        self.phase = self.guard.phase = "speech-roundtrips"
        for label, speech, words in (
            (
                "zh",
                "明天下午三点在图书馆开会，请带上蓝色笔记本。",
                ["三点", "图书馆", "蓝色", "笔记本"],
            ),
            (
                "en",
                "The meeting starts at three in the library. Please bring the blue notebook.",
                ["three", "library", "blue", "notebook"],
            ),
        ):
            c = case("tts-" + label, model=TTS, check="audio", expected=None)
            c["payload"] = {
                "model": TTS,
                "messages": [{"role": "assistant", "content": speech}],
                "audio": {"format": "wav", "voice": "mimo_default"},
                "stream": False,
            }
            r = await self.call(c)
            if not r.get("passed") or not 0 < r.get("audio_seconds", 999) <= 30:
                continue
            audio = base64.b64encode((self.root / r["audio_file"]).read_bytes()).decode()
            audio_part = {
                "type": "input_audio",
                "input_audio": {"data": "data:audio/wav;base64," + audio},
            }
            c = case(
                "asr-" + label,
                model=ASR,
                stream=True,
                check="transcript",
                expected=words,
                messages=[{"role": "user", "content": [audio_part]}],
            )
            c["payload"].pop("thinking")
            c["payload"].pop("max_completion_tokens")
            await self.call(c)

            c = case(
                "audio-understanding-" + label,
                stream=True,
                check="json",
                expected={"place": "library", "item": "blue notebook"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Listen and return "
                                'only English JSON {"place":...,"item":...}. '
                                "Where and what to bring?",
                            },
                            audio_part,
                        ],
                    }
                ],
            )
            await self.call(c)

    async def focused_workflows(self):
        self.phase = self.guard.phase = "calculator-workflows"
        for model in MODELS:
            for dataset in ("sales-240", "knapsack-12"):
                c = case(
                    f"calculator-{model}-{dataset}",
                    f"Use calculate_fixture for dataset {dataset}. After the tool result, "
                    "return that result unchanged as a JSON object.",
                    model=model,
                    stream=True,
                    check="fixture_tool",
                    expected=dataset,
                )
                c["payload"]["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": "calculate_fixture",
                            "description": "Compute the exact answer for a fixed dataset.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "dataset": {
                                        "type": "string",
                                        "enum": ["sales-240", "knapsack-12"],
                                    }
                                },
                                "required": ["dataset"],
                                "additionalProperties": False,
                            },
                            "strict": True,
                        },
                    }
                ]
                r = await self.call(c)
                if not r.get("passed"):
                    continue
                f = r["function_calls"][0]
                args = json.loads(f["arguments"])
                if args != {"dataset": dataset}:
                    raise ValueError("Unapproved calculator arguments")
                answer = calculate_fixture(dataset)
                following = copy.deepcopy(c)
                following.update(name=c["name"] + "-followup", check="json", expected=answer)
                following["payload"]["response_format"] = {"type": "json_object"}
                following["payload"]["messages"] += [
                    {
                        "role": "assistant",
                        "content": r["text"],
                        "tool_calls": [
                            {
                                "id": f["id"],
                                "type": "function",
                                "function": {"name": f["name"], "arguments": f["arguments"]},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": f["id"], "content": json.dumps(answer)},
                ]
                await self.call(following)
        self.phase = self.guard.phase = "deduplication-code"
        for model in MODELS:
            spec = make("coding", "b", 1)
            c = case(
                f"dedupe-code-{model}",
                spec["prompt"],
                model=model,
                thinking=True,
                stream=True,
                cap=8192,
                check="application",
                expected=None,
                application=spec,
            )
            r = await self.call(c)
            r["application"] = {"scenario": "coding", "variant": "b", "turn": 1}


async def run(args):
    maximum_micros = budget_micros(getattr(args, "budget_cny", 20))
    source = config_directory()
    paths = [source / n for n in ("server.json", "connections.json", "client.json")]
    fingerprints = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}
    server = load_config(source / "server.json", ServerConfig)
    if server.base_url != ORIGIN + "/v1":
        raise ValueError("Configured official MiMo connection required")
    registry_path = source / "connections.json"
    registry = (
        load_config(registry_path, ConnectionsConfig)
        if registry_path.exists()
        else ConnectionsConfig()
    )
    configured = legacy_connection(server, registry)
    needed = {*MODELS, TTS, ASR} if getattr(args, "suite", "full") == "full" else set(MODELS)
    if not configured.enabled or not needed <= set(configured.allowed_models):
        raise ValueError("Requested test models must already be explicitly enabled")
    key = server.api_key.get_secret_value()
    prior_attempts = prior_reserved = 0
    if getattr(args, "prior_run", None):
        old_budget = json.loads((args.prior_run / "budget.json").read_text())
        old_report = json.loads((args.prior_run / "report.json").read_text())
        if not old_report.get("temporary_profiles_removed"):
            raise ValueError("Prior service must finish and clean up before continuation")
        if old_budget.get("stop_reason") not in (None, "operator_stop", "harness_parser_error"):
            raise ValueError("Cannot continue an authentication, quota or spending stop")
        prior_attempts = old_report.get("prior_attempts", 0) + old_budget["attempts"]
        prior_reserved = old_report.get("prior_reserved_micros", 0) + old_budget["reserved_micros"]
        old_maximum = old_report.get("total_budget_micros", MAX_MICROS)
        if maximum_micros > old_maximum:
            raise ValueError("A continuation cannot increase its original budget")
        if prior_attempts >= MAX_ATTEMPTS or prior_reserved >= maximum_micros:
            raise ValueError("Cumulative budget exhausted")
    args.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    guard = MiMoGuard(
        args.work_dir,
        key,
        maximum=maximum_micros - prior_reserved,
        attempts=MAX_ATTEMPTS - prior_attempts,
    )
    runner = Runner(args.work_dir, guard)
    runner.report.update(
        prior_attempts=prior_attempts,
        prior_reserved_micros=prior_reserved,
        total_budget_micros=maximum_micros,
        suite=getattr(args, "suite", "full"),
    )
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SELARK_") and not k.endswith(("_API_KEY", "_TOKEN"))
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        tempfile.TemporaryDirectory(prefix="selark-mimo-extended-") as tmp,
    ):
        profile = Path(tmp)
        initialize(profile, "unused-test-placeholder", models=list(MODELS))
        with Runtime(profile) as runtime:
            connection = ConnectionConfig(
                connection_id="mimo-extended",
                name="Temporary MiMo acceptance",
                provider="mimo",
                base_url=ORIGIN + "/v1",
                api_key=key,
                allowed_models=list(MODELS),
                default_model=FLASH,
                timeout_seconds=300,
            )
            runtime.connection_settings = lambda: [connection]
            config = runtime.settings()
            config.limits = config.limits.model_copy(
                update={
                    "max_concurrent_requests": 8,
                    "max_queued_requests": 8,
                    "queue_timeout_seconds": 90,
                    "request_timeout_seconds": 330,
                    "send_timeout_seconds": 90,
                }
            )
            app = create_app(config, runtime=runtime)
            tokens = [
                runtime.store.create_token(
                    f"mimo-scenario-{i}",
                    policy={
                        "allowed_connections": ["mimo-extended"],
                        "max_output_tokens": MAX_OUTPUT,
                    },
                )
                for i in range(8)
            ]
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            try:
                async with serve(app, runtime, sock), AsyncExitStack() as stack:
                    app.state.http.event_hooks["request"].append(guard.before)
                    app.state.http.event_hooks["response"].append(guard.after)
                    runner.direct = await stack.enter_async_context(
                        httpx.AsyncClient(
                            timeout=330,
                            trust_env=False,
                            follow_redirects=False,
                            headers={"api-key": key, "anthropic-version": "2023-06-01"},
                            event_hooks={"request": [guard.before], "response": [guard.after]},
                        )
                    )
                    metadata = await runner.direct.get(ORIGIN + "/v1/models")
                    runner.report["metadata_status"] = metadata.status_code
                    if metadata.status_code != 200:
                        raise RuntimeError("Model metadata failed")
                    runner.report["available_models"] = [
                        m["id"] for m in metadata.json().get("data", [])
                    ]
                    for token in tokens:
                        runner.gateway_clients.append(
                            await stack.enter_async_context(
                                httpx.AsyncClient(
                                    base_url=url,
                                    trust_env=False,
                                    timeout=360,
                                    limits=httpx.Limits(max_connections=32, keepalive_expiry=0.5),
                                    headers={"Authorization": "Bearer " + token["token"]},
                                )
                            )
                        )

                    async def health():
                        response = await runner.gateway_clients[0].get("/health")
                        response.raise_for_status()
                        return response.json()

                    runner.health = health
                    suite = getattr(args, "suite", "full")
                    plan = static_phases()
                    if getattr(args, "suite", "full") == "full":
                        await runner.phase_cases(*plan[0])
                        await runner.phase_cases("vision", 3, vision_cases(args.work_dir))
                        await runner.audio_workflows()
                    if suite == "stress":
                        plan = stress_phases()
                    elif suite == "focus":
                        await runner.focused_workflows()
                        plan = [("focused-controls", 4, focused_cases(args.work_dir))]
                    else:
                        await runner.tool_workflows()
                        await runner.conversations()
                        plan = plan[1:]
                    for name, concurrency, cases in plan:
                        await runner.phase_cases(name, concurrency, cases)
                        if guard.stop_reason:
                            break
                    slow = [
                        case(
                            f"slow-reader-{m}",
                            "Write 200 words explaining cache invalidation.",
                            model=m,
                            route="gateway",
                            stream=True,
                            cap=1024,
                            read_delay=0.01,
                            check="substantive",
                            expected={"min_characters": 300, "terms": ["cache"]},
                        )
                        for m in MODELS
                    ]
                    await runner.phase_cases("slow-readers", 2, slow)
                    negative = []
                    for i, extra in enumerate(
                        (
                            {"tools": [ADD_TOOL]},
                            {"thinking": {"type": "enabled"}},
                            {"response_format": {"type": "json_object"}},
                            {"max_completion_tokens": 0},
                        )
                    ):
                        c = case(
                            f"gateway-reject-{i}", route="gateway", check="reject", expected=None
                        )
                        c["payload"].update(extra)
                        negative.append(c)
                    await runner.phase_cases("gateway-unsupported", 4, negative)
                    if suite == "focus" and not guard.stop_reason:
                        search = case(
                            "web-search-capability",
                            "Search for the official SQLite documentation "
                            "about write-ahead logging. "
                            "Return its official URL and a short summary with source attribution.",
                            stream=True,
                            check="web",
                            expected=None,
                        )
                        search["payload"]["tools"] = [
                            {
                                "type": "web_search",
                                "force_search": True,
                                "max_keyword": 1,
                                "limit": 1,
                            }
                        ]
                        search["payload"]["tool_choice"] = "auto"
                        await runner.phase_cases("web-search-capability", 1, [search])

                    async def idle():
                        h = await health()
                        return (
                            h["capacity"]["admitted"] == 0 and h["storage"]["pending_records"] == 0
                        )

                    await wait_for(idle)
                    runner.report["records"] = runtime.store.records({}, limit=1000)
                    runner.report["final_health"] = await health()
                runner.report["recovery"] = runtime.recover_records()
                runner.report["completed"] = True
            finally:
                runner.report.update(
                    source_configuration_unchanged=all(
                        hashlib.sha256(p.read_bytes()).hexdigest() == h
                        for p, h in fingerprints.items()
                    ),
                    temporary_files_contain_provider_key=any(
                        key.encode() in p.read_bytes() for p in profile.rglob("*") if p.is_file()
                    ),
                    outgoing_attempts=len(guard.events),
                    reserved_micros=guard.reserved,
                    stop_reason=guard.stop_reason,
                )
                runner.save()
    runner.report.update(temporary_profiles_removed=True, finished_at=utc_now())
    runner.save()
    print(
        "FINAL "
        + json.dumps(
            {
                k: runner.report[k]
                for k in (
                    "completed",
                    "outgoing_attempts",
                    "reserved_micros",
                    "stop_reason",
                    "source_configuration_unchanged",
                )
            }
        ),
        flush=True,
    )
    return runner.report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--prior-run", type=Path)
    parser.add_argument("--suite", choices=("full", "remaining", "focus", "stress"), default="full")
    parser.add_argument("--budget-cny", type=Decimal, default=Decimal("20"))
    args = parser.parse_args()
    if not args.execute:
        parser.error("Real API calls require --execute")
    asyncio.run(run(args))
