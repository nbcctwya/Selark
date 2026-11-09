"""The offline burst harness must expose transport failures, never retry or hide them."""

import asyncio
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from scripts.verify_resource_matrix import mixed_burst

from .four_resources import FourUpstreams, app_for, auth, caller, payload
from .support.resources import resources as resources


@pytest.fixture
def qualification_evidence():
    from scripts.qualify_relay import RECOVERY_CHECKS, STEPS

    signature = {
        **dict.fromkeys(("runtime", "installed_runtime", "harness", "dependencies_lock"), "a" * 64),
        "python": "3.12.2",
        "platform": "Linux",
        "machine": "x86_64",
    }
    # Hand-calculated ranks: 150 negative samples, then 135 at 5 ms, 12 at 8 ms,
    # and 3 at 9 ms. p50=-1, p95=5 and p99=8; negative differences must survive.
    timings = {
        "pairs": [
            {"direct_ms": 100, "relay_ms": value}
            for value in [99] * 150 + [105] * 135 + [108] * 12 + [109] * 3
        ],
        "overhead_ms": {"p50": -1, "p95": 5, "p99": 8},
        "negative_differences": 150,
    }
    relay = {
        "python": "3.12.2",
        "platform": "Linux",
        "machine": "x86_64",
        "samples_per_mode": 300,
        "warmup_pairs_per_mode": 5,
        "concurrency": 1,
        "chunked_sse": False,
        "loopback_proxy_bypassed": True,
        "real_provider_requests": 0,
        "extra_generations": 0,
        "fake_direct_generations": 610,
        "fake_relay_generations": 610,
        "reported_relay_tokens": 8540,
        "forwarded_payloads_unchanged": True,
        "idle": {
            "seconds": 30,
            "cpu_seconds": 0.03,
            "rss_bytes": 60 * 1048576,
            "upstream_requests": 0,
        },
        **{
            mode: copy.deepcopy(timings)
            for mode in ("nonstream", "stream_first_text", "stream_complete")
        },
    }
    invariants = {
        "requests": 10,
        "fake_generation_requests": 11,
        "reported_tokens": 154,
        "real_provider_requests": 0,
        "forwarded_messages_unchanged": True,
        "idle_seconds": 2,
        "idle_journal_scans": 0,
        "jwt_loaded": False,
    }
    recovery = {
        "real_mimo_requests": 0,
        "simulated_upstream_requests": 9,
        "checks": [
            {"check": name, "passed": True, "known_total_tokens": 56, "upstream_calls": 0}
            for name in sorted(RECOVERY_CHECKS)
        ],
    }
    return {
        "schema": 1,
        "complete": True,
        "fingerprint": signature,
        "final_fingerprint": copy.deepcopy(signature),
        "steps": [
            {
                "name": name,
                "before": copy.deepcopy(signature),
                "after": copy.deepcopy(signature),
                "exit_code": 0,
                "report": copy.deepcopy(
                    invariants
                    if name == "invariants"
                    else recovery
                    if name == "recovery"
                    else relay
                ),
            }
            for name, *_ in STEPS
        ],
    }


def test_qualification_recomputes_raw_pairs_and_ignores_saved_flags(qualification_evidence):
    from scripts.measure_relay import BUDGET
    from scripts.qualify_relay import POLICY, assess

    assert (POLICY["p95_ms"], POLICY["rss_mib"], POLICY["cpu"]) == (
        BUDGET["relay_p95_ms"],
        BUDGET["idle_rss_mib"],
        BUDGET["idle_cpu_percent"],
    )
    for step in qualification_evidence["steps"][2:]:
        step["report"]["budget_checks"] = {"nonstream_p95": False}
    result = assess(qualification_evidence)
    assert result["qualified"] and all(result["groups"].values())
    assert result["rounds"][0]["nonstream"] == {"p50": -1, "p95": 5, "p99": 8}

    # One bad round must fail even when saved flags and the median look good.
    report = qualification_evidence["steps"][2]["report"]
    data = report["nonstream"]
    for pair in data["pairs"]:
        pair["relay_ms"] = pair["direct_ms"] + 11
    data.update(overhead_ms={"p50": 11, "p95": 11, "p99": 11}, negative_differences=0)
    report["budget_checks"] = {"nonstream_p95": True}
    report["budgets"] = {"relay_p95_ms": 1000}
    result = assess(qualification_evidence)
    assert result["groups"]["evidence"] and not result["qualified"]
    assert result["failed_checks"] == [
        {"group": "performance", "check": "relay-1_nonstream_p95", "passed": False}
    ]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("complete",), False),
        (("final_fingerprint", "runtime"), "b" * 64),
        (("fingerprint", "installed_runtime"), "b" * 64),
        (("steps", 2, "exit_code"), 1),
        (("steps", 2, "error_type"), "TimeoutExpired"),
        (("steps", 0, "report", "idle_journal_scans"), 1),
        (("steps", 0, "report", "fake_generation_requests"), 12),
        (("steps", 1, "report", "checks"), []),
        (("steps", 2, "report", "profile"), {}),
        (("steps", 2, "report", "python"), "3.13.0"),
        (("steps", 2, "report", "samples_per_mode"), 299),
        (("steps", 2, "report", "nonstream", "pairs"), []),
        (("steps", 2, "report", "nonstream", "pairs", 0, "relay_ms"), float("nan")),
        (("steps", 2, "report", "nonstream", "pairs", 0, "direct_ms"), -1),
        (("steps", 2, "report", "nonstream", "overhead_ms", "p95"), 0),
        (("steps", 2, "report", "reported_relay_tokens"), None),
        (("steps", 2, "report", "idle", "seconds"), 2),
        (("steps", 2, "report", "idle", "rss_bytes"), None),
        (("steps", 2, "report", "idle", "cpu_seconds"), 1),
    ],
)
def test_qualification_rejects_missing_or_damaged_evidence(qualification_evidence, path, value):
    from scripts.qualify_relay import assess

    target = qualification_evidence
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = assess(qualification_evidence)
    assert not result["qualified"] and result["failed_checks"]


def test_qualification_preserves_failed_collection_and_refuses_overwrite(
    tmp_path, monkeypatch, capsys
):
    from scripts import qualify_relay

    def unavailable(_):
        raise subprocess.TimeoutExpired("synthetic import probe", 10)

    monkeypatch.setattr(qualify_relay, "fingerprint", unavailable)
    output = tmp_path / "qualification"
    assert not qualify_relay.collect(output)["qualified"]
    evidence = json.loads((output / "evidence.json").read_text())
    assert not evidence["complete"] and evidence["error_type"] == "TimeoutExpired"
    monkeypatch.setattr(
        "sys.argv", ["qualify_relay", "--check-evidence", str(output / "evidence.json")]
    )
    assert qualify_relay.main() == 2
    assert not json.loads(capsys.readouterr().out)["qualified"]
    original = (output / "evidence.json").read_bytes()
    monkeypatch.setattr("sys.argv", ["qualify_relay", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        qualify_relay.main()
    assert error.value.code == 2 and (output / "evidence.json").read_bytes() == original


@pytest.mark.skipif(sys.platform != "linux", reason="Reference collector uses Linux process groups")
@pytest.mark.parametrize("parent_mode", ["ignore_interrupt", "handle_interrupt", "exit_failed"])
def test_qualification_timeout_cleans_its_process_group(tmp_path, parent_mode):
    from scripts.qualify_relay import run_step

    identity = tmp_path / "process-group"
    # The child ignores SIGINT; cleanup must work even if its parent exits first.
    child = "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)"
    parent = (
        "import os,signal,subprocess,sys,time; from pathlib import Path; "
        + (
            "signal.signal(signal.SIGINT, signal.SIG_IGN); "
            if parent_mode == "ignore_interrupt"
            else ""
        )
        + f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        f"Path({str(identity)!r}).write_text(str(os.getpid())); "
        + ("sys.exit(7)" if parent_mode == "exit_failed" else "time.sleep(60)")
    )
    started = time.monotonic()
    if parent_mode == "exit_failed":
        assert (
            run_step([sys.executable, "-c", parent], tmp_path / "timeout.log", os.environ.copy(), 1)
            == 7
        )
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            run_step([sys.executable, "-c", parent], tmp_path / "timeout.log", os.environ.copy(), 1)
    assert time.monotonic() - started < 15
    group = int(identity.read_text())
    # Linux can briefly retain a dead orphan as a zombie; it cannot execute work.
    for _ in range(100):
        active = []
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat.read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                active.append(stat.parent.name)
        if not active:
            break
        time.sleep(0.01)
    assert not active


@pytest.mark.parametrize("chunked", [False, True])
def test_completed_streams_reuse_tcp_connections(chunked):
    from scripts.measure_relay import run

    report = run(samples=2, idle_seconds=0.01, chunked_sse=chunked)
    # One direct and one relayed upstream connection, retained across both modes.
    assert report["upstream_connections"] == {"nonstream": 2, "stream": 0}


def test_latency_report_uses_paired_signed_differences_and_nearest_rank():
    from scripts.measure_relay import comparison, distribution

    pairs = [
        {"direct_ms": 100, "relay_ms": 99},
        {"direct_ms": 1, "relay_ms": 4},
        {"direct_ms": 1, "relay_ms": 10},
        {"direct_ms": 1, "relay_ms": 15},
    ]
    result = comparison(pairs)
    assert result["pairs"] == pairs and result["negative_differences"] == 1
    assert result["overhead_ms"] == {"p50": 3, "p95": 14, "p99": 14}
    assert result["overhead_ms"]["p95"] != result["relay_ms"]["p95"] - result["direct_ms"]["p95"]
    assert distribution(range(1, 101)) == {"p50": 50, "p95": 95, "p99": 99}


def test_unmet_performance_budget_is_reported_and_strict_mode_fails(tmp_path, monkeypatch, capsys):
    from scripts import measure_relay

    report = {
        "idle": {"cpu_percent": None, "rss_bytes": 58 * 1048576},
        "nonstream": {"overhead_ms": {"p95": 10.01}},
        "stream_first_text": {"overhead_ms": {"p95": 10}},
    }
    report["budget_checks"] = measure_relay.budget_checks(report)
    assert report["budget_checks"] == {
        "idle_rss": True,
        "idle_cpu": None,
        "nonstream_p95": False,
        "stream_first_text_p95": True,
    }
    output = tmp_path / "measurement.json"
    monkeypatch.setattr(measure_relay, "run", lambda **kwargs: report)
    for strict, expected in ((False, 0), (True, 1)):
        monkeypatch.setattr(
            "sys.argv",
            ["measure_relay", "--output", str(output)] + (["--check-budget"] if strict else []),
        )
        assert measure_relay.main() == expected
        assert json.loads(output.read_text())["budget_checks"] == report["budget_checks"]
        assert json.loads(capsys.readouterr().out)["budget_checks"]["nonstream_p95"] is False

    def fail(**kwargs):
        kwargs["report"].update(progress={"route": "relay", "pair": 12}, partial_pairs=[])
        raise httpx.ReadTimeout("Synthetic measurement failure")

    monkeypatch.setattr(measure_relay, "run", fail)
    with pytest.raises(httpx.ReadTimeout):
        measure_relay.main()
    failed = json.loads(output.read_text())
    assert failed["failed"] and failed["error_type"] == "ReadTimeout"
    assert failed["progress"] == {"route": "relay", "pair": 12}
    assert "budget_checks" not in failed  # The previous success cannot survive a failed run.


@pytest.mark.parametrize("fault", [None, "transport", "primary_and_transport"])
def test_burst_preserves_failure_and_waits_for_every_client(resources, tmp_path, fault):
    async def scenario():
        upstream = FourUpstreams()
        app = app_for(resources, upstream, max_concurrent_requests=4, max_queued_requests=8)
        tokens = [caller(resources, "A"), caller(resources, "B")]
        issued, settled, pools = [], [], set()
        output = tmp_path / "burst.json"
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as c,
        ):

            async def post(identity, *, who, session, http, **options):
                index = len(issued)
                issued.append(index)
                pools.add(id(http))
                try:
                    if fault and index == 9:
                        raise httpx.ReadError("synthetic transport failure; do not retry")
                    return await c.post(
                        "/v1/chat/completions",
                        headers=auth(tokens[who]),
                        json=payload(identity, **options) | {"session_id": session},
                    )
                finally:
                    settled.append(index)

            async def health():
                if fault == "primary_and_transport":
                    raise RuntimeError("synthetic primary failure")
                return (await c.get("/health")).json()

            async def idle():
                return app.state.admission.health()["admitted"] == 0

            args = (
                0,
                "http://127.0.0.1",
                post,
                health,
                idle,
                upstream,
                app.state.admission,
                output,
            )
            if fault:
                error = RuntimeError if fault == "primary_and_transport" else AssertionError
                with pytest.raises(error):
                    await mixed_burst(*args)
                evidence = json.loads(output.with_suffix(".failure.json").read_text())
                assert evidence["exception"] == error.__name__
                assert evidence["clients_submitted"] == evidence["clients_settled"] == 64
                assert evidence["client_errors"] == [{"client": 9, "exception": "ReadError"}]
                assert evidence["real_generation_requests"] == 0
            else:
                report = await mixed_burst(*args)
                assert report["passed"] and report["client_transport_errors"] == 0
                assert report["generation_retries"] == 0
                assert not output.with_suffix(".failure.json").exists()
            assert sorted(issued) == sorted(settled) == list(range(64))
            assert len(pools) == 1
            assert app.state.admission.health()["admitted"] == 0
            assert not app.state.admission.plans and not app.state.admission.callers
            total = resources.store.summary({})["summary"]
            assert total["upstream_attempts"] == len(upstream.calls)
            assert total["known_tokens"]["total_tokens"] == 14 * len(upstream.calls)

    asyncio.run(scenario())
