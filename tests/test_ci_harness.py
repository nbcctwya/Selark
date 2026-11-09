"""The offline burst harness must expose transport failures, never retry or hide them."""

import asyncio
import json

import httpx
import pytest

from scripts.verify_resource_matrix import mixed_burst

from .four_resources import FourUpstreams, app_for, auth, caller, payload
from .support.resources import resources as resources


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
