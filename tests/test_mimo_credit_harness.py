"""Spending controls must fail closed before any real endpoint is used."""

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from selark.app import create_app
from selark.config import _write_private, initialize
from selark.runtime import Runtime

from .conftest import completion

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("mimo_credit", SCRIPTS / "verify_mimo_credit.py")
harness = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(harness)
finally:
    sys.path.remove(str(SCRIPTS))

PAYLOAD = {
    "model": harness.MODEL,
    "messages": [{"role": "user", "content": "synthetic"}],
    "thinking": {"type": "disabled"},
    "max_completion_tokens": 8,
}


def budget(path, maximum, **changes):
    _write_private(
        path / "budget.json",
        {
            "maximum_micros": maximum,
            "reserved_micros": 0,
            "attempts": 0,
            "max_attempts": 5,
            "deadline": time.time() + 60,
            "reservations": [],
            **changes,
        },
    )


def test_global_data_override_cannot_redirect_acceptance_to_an_existing_ledger(
    tmp_path, monkeypatch
):
    original = tmp_path / "original.sqlite3"
    original.write_bytes(b"must remain untouched")
    monkeypatch.setenv("SELARK_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="Unset SELARK_DATA_DIR"):
        harness.Acceptance(SimpleNamespace(work_dir=tmp_path / "acceptance"))
    assert original.read_bytes() == b"must remain untouched"
    assert not (tmp_path / "acceptance").exists()


def test_concurrent_reservations_persist_before_send_and_survive_restart(tmp_path):
    cost = harness.reserve_micros(PAYLOAD)
    budget(tmp_path, cost * 2)
    calls = []

    def upstream(request):
        state = json.loads((tmp_path / "budget.json").read_text())
        assert state["reserved_micros"] >= (len(calls) + 1) * cost
        calls.append(request)
        return httpx.Response(200, json={})

    async def run():
        for _ in range(2):
            transport = harness.CreditTransport(tmp_path, httpx.MockTransport(upstream))
            async with httpx.AsyncClient(transport=transport) as client:
                results = await asyncio.gather(
                    *(
                        client.post(harness.ORIGIN + "/chat/completions", json=PAYLOAD)
                        for _ in range(3)
                    ),
                    return_exceptions=True,
                )
                assert any(isinstance(result, httpx.RequestError) for result in results)

    asyncio.run(run())
    assert len(calls) == 2
    state = json.loads((tmp_path / "budget.json").read_text())
    assert state["reserved_micros"] == 2 * cost and state["attempts"] == 2


@pytest.mark.parametrize("stop", ["deadline", "file", "attempts"])
def test_stop_conditions_prevent_egress(tmp_path, stop):
    budget(tmp_path, 1000000, **({"deadline": 0} if stop == "deadline" else {}))
    if stop == "file":
        (tmp_path / "STOP").touch()
    elif stop == "attempts":
        budget(tmp_path, 1000000, attempts=5)

    def forbidden(request):
        pytest.fail("Stopped acceptance must not reach the network")

    async def run():
        async with httpx.AsyncClient(
            transport=harness.CreditTransport(tmp_path, httpx.MockTransport(forbidden))
        ) as client:
            with pytest.raises(httpx.RequestError):
                await client.post(harness.ORIGIN + "/chat/completions", json=PAYLOAD)
            with pytest.raises(httpx.RequestError):
                await client.post("https://unapproved.invalid/v1/chat/completions", json=PAYLOAD)

    asyncio.run(run())


def test_network_failure_does_not_release_its_spending_reservation(tmp_path):
    cost = harness.reserve_micros(PAYLOAD)
    budget(tmp_path, cost)
    calls = []

    def failure(request):
        calls.append(request)
        raise httpx.ReadTimeout("simulated unknown outcome")

    async def run():
        async with httpx.AsyncClient(
            transport=harness.CreditTransport(tmp_path, httpx.MockTransport(failure))
        ) as client:
            for _ in range(2):
                with pytest.raises(httpx.RequestError):
                    await client.post(harness.ORIGIN + "/chat/completions", json=PAYLOAD)

    asyncio.run(run())
    assert len(calls) == 1
    assert json.loads((tmp_path / "budget.json").read_text())["reserved_micros"] == cost
    for change in ({"model": "other"}, {"tools": []}, {"max_completion_tokens": 8193}):
        with pytest.raises(ValueError):
            harness.reserve_micros(PAYLOAD | change)


def test_acceptance_checks_real_ledger_through_simulated_upstream(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    profile = tmp_path / "profile"
    initialize(profile, "fake-acceptance-only", models=[harness.MODEL])
    budget(tmp_path, 1000000)
    reply = completion() | {"model": harness.MODEL}
    reply["usage"]["prompt_tokens_details"] = {"cached_tokens": 0}
    with Runtime(profile) as runtime:
        runtime.edit_resource("price", ("mimo-default", harness.MODEL), harness.PRICE)
        caller = runtime.store.create_token(
            "test",
            policy={
                "allowed_connections": ["mimo-default"],
                "allowed_models": [f"mimo-default::{harness.MODEL}"],
            },
        )
        denied = runtime.store.create_token("denied", policy={"allowed_connections": []})
        transport = harness.CreditTransport(
            tmp_path, httpx.MockTransport(lambda _: httpx.Response(200, json=reply))
        )
        app = create_app(runtime.settings(), runtime=runtime, transport=transport)
        driver = harness.Acceptance(
            SimpleNamespace(
                work_dir=tmp_path,
                verified_gift_cny=11,
                max_reserve_cny=8,
            )
        )
        driver.tokens = {"primary": caller["token"], "denied": denied["token"]}
        driver.url = "http://acceptance.test"

        async def run():
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http,
            ):
                driver.http = http
                await driver.local_checks()
                result = await driver.call("smoke", "synthetic", cap=16)
                assert result["transport_passed"]
                assert result["record"]["resources"]["estimated_cost"] is not None
                (tmp_path / "STOP").touch()
                response = await http.post(
                    driver.url + "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "not sent"}]},
                    headers={"Authorization": "Bearer " + caller["token"]},
                )
                assert response.status_code == 502
                stopped = next(
                    row
                    for row in driver.records()
                    if row["request_id"] == response.headers["x-request-id"]
                )
                assert stopped["upstream_attempts"] == 0
                assert stopped["upstream_result"] == "not_started"
                assert stopped["resources"] is None
                assert json.loads((tmp_path / "budget.json").read_text())["attempts"] == 1

        asyncio.run(run())
