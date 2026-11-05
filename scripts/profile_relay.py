"""Opt-in, process-local timing probes for isolated relay measurements.

Loaded only by measure_relay --profile. No request/response text, credentials or
configuration contents are collected. Spans can nest: do not sum their percentiles.
The ordinary gateway never imports this module or installs these wrappers.
"""

import contextvars
import functools
import json
import time
from pathlib import Path

active = contextvars.ContextVar("relay_trace", default=None)


def now():
    return time.perf_counter_ns()


def span(name, start, end=None):
    trace = active.get()
    if trace is not None:
        trace["spans"].append(
            {
                "name": name,
                "start_ms": (start - trace["start"]) / 1e6,
                "duration_ms": ((end or now()) - start) / 1e6,
            }
        )


def mark(name):
    trace = active.get()
    if trace is not None:
        trace["marks"].setdefault(name, (now() - trace["start"]) / 1e6)


def wrap_sync(cls, method, label):
    original = getattr(cls, method)

    @functools.wraps(original)
    def measured(*args, **kwargs):
        start = now()
        try:
            return original(*args, **kwargs)
        finally:
            span(label, start)

    setattr(cls, method, measured)


def install(output):
    import httpx

    from selark.app import RequestMiddleware
    from selark.persistence import AsyncLedger, Journal
    from selark.providers.mimo import MiMoProvider
    from selark.runtime import Runtime
    from selark.storage import Store

    output = Path(output)
    # A dedicated trace file, never append a new run onto an old successful run.
    with output.open("x", encoding="utf-8"):
        pass
    middleware = RequestMiddleware.__call__

    async def measured_request(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/v1/chat/completions":
            return await middleware(self, scope, receive, send)
        trace = {"start": now(), "spans": [], "marks": {}}
        token = active.set(trace)

        async def measured_send(message):
            if message["type"] == "http.response.start":
                mark("response_headers")
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    mark("first_body")
                if not message.get("more_body"):
                    mark("last_body")
            await send(message)

        try:
            return await middleware(self, scope, receive, measured_send)
        finally:
            mark("settled")
            state = scope.get("state", {})
            trace["stream"] = bool(getattr(state.get("chat_input"), "stream", False))
            trace["upstream_attempts"] = state.get("upstream_attempts", 0)
            trace["delivery_status"] = state.get("delivery_status")
            trace.pop("start")
            active.reset(token)
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace) + "\n")

    RequestMiddleware.__call__ = measured_request
    for method in ("resources", "connection_settings"):
        wrap_sync(Runtime, method, "config." + method)
    for method in ("authenticate", "policy", "begin_request", "attempt", "finish"):
        wrap_sync(Store, method, "store." + method)
    for method in ("save", "remove", "entries"):
        wrap_sync(Journal, method, "journal." + method)

    original_call = AsyncLedger.call

    async def measured_call(self, method, *args, **kwargs):
        start = now()
        try:
            return await original_call(self, method, *args, **kwargs)
        finally:
            span("await_store." + method, start)
            if method == "authenticate":
                mark("authenticated")
            elif method == "attempt":
                mark("admitted")

    AsyncLedger.call = measured_call
    original_work = AsyncLedger.wait_work

    async def measured_work(pool, work):
        submitted, context = now(), contextvars.copy_context()

        def measured():
            started = now()
            span("executor.queue", submitted, started)
            try:
                return work()
            finally:
                span("executor.work", started)

        return await original_work(pool, functools.partial(context.run, measured))

    AsyncLedger.wait_work = staticmethod(measured_work)
    original_persist = AsyncLedger.persist

    async def measured_persist(self, *args, settled, **kwargs):
        start = now()
        try:
            return await original_persist(self, *args, settled=settled, **kwargs)
        finally:
            span("persist.settled" if settled else "persist.checkpoint", start)

    AsyncLedger.persist = measured_persist
    original_request = MiMoProvider.request

    async def measured_upstream(self, *args, **kwargs):
        mark("upstream_start")
        start = now()
        try:
            return await original_request(self, *args, **kwargs)
        finally:
            span("upstream.headers", start)
            mark("upstream_headers")

    MiMoProvider.request = measured_upstream
    original_send = httpx.AsyncClient.send

    async def measured_http(self, request, **kwargs):
        starts = {}

        async def trace_event(name, info):
            event, _, state = name.rpartition(".")
            if state == "started":
                starts[event] = now()
            elif event in starts:
                span("http." + event, starts.pop(event))

        request.extensions["trace"] = trace_event
        return await original_send(self, request, **kwargs)

    httpx.AsyncClient.send = measured_http


def summarize(path):
    from scripts.measure_relay import distribution

    traces = [json.loads(line) for line in Path(path).read_text().splitlines()]
    result = {}
    for stream in (False, True):
        # Match the latency measurement's five warm-up requests per mode.
        selected = [trace for trace in traces if trace["stream"] == stream][5:]
        names = sorted({s["name"] for trace in selected for s in trace["spans"]})
        marks = sorted({name for trace in selected for name in trace["marks"]})
        boundaries = [
            None,
            "authenticated",
            "upstream_start",
            "upstream_headers",
            "first_body",
            "last_body",
            "settled",
        ]
        phases = [
            "authentication",
            "routing_and_admission",
            "upstream_to_headers",
            "headers_to_first_body",
            "body_delivery",
            "settlement",
        ]
        result["stream" if stream else "nonstream"] = {
            "requests": len(selected),
            "phases_ms": {
                name: distribution(
                    [
                        trace["marks"][end] - (trace["marks"][start] if start else 0)
                        for trace in selected
                        if end in trace["marks"] and (start is None or start in trace["marks"])
                    ]
                )
                for name, start, end in zip(phases, boundaries[:-1], boundaries[1:], strict=True)
            },
            "marks_ms": {
                name: distribution(
                    [trace["marks"][name] for trace in selected if name in trace["marks"]]
                )
                for name in marks
            },
            "inclusive_spans_ms": {
                name: distribution(
                    [
                        sum(s["duration_ms"] for s in trace["spans"] if s["name"] == name)
                        for trace in selected
                    ]
                )
                for name in names
            },
        }
    return result
