"""Protocol evidence, reasoning-token accounting and IPv6 health regressions."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from selark import dashboard_data as data
from selark.config import _write_private, server_document
from selark.runtime import Runtime, capability_fingerprint, fingerprint
from selark.usage import MAX_COUNT, inconsistent_usage, observed_token_floor, response_usage

from .support.governance import PAYLOAD, allocation, app_for, auth, finished, price
from .support.governance import runtime as runtime
from .support.protocol import completion, event, sse


def test_legacy_protocol_success_is_corrected_without_rewriting_history(runtime):
    caller = runtime.store.create_token("legacy-protocol")
    config = runtime.connection_settings()[0]
    allocation(runtime, caller, "legacy-protocol")
    runtime.store.finish(
        "legacy-protocol",
        state=finished(
            caller,
            "legacy-protocol",
            fingerprint=fingerprint(config),
            model_fingerprint=capability_fingerprint(config),
            protocol="chat-completions",
            capability="text_nonstream",
            error_code="upstream_protocol_error",
        ),
        http_status=502,
        duration_ms=1,
    )
    # Older nonstream failures kept only error_code and incorrectly stored success.
    with runtime.store.transaction() as db:
        db.execute("UPDATE verifications SET state='success'")
    with Runtime(runtime.directory, readonly=True) as reader:
        assert reader.connection()["verification"]["state"] == "failure"
        model = reader.models("mimo-default")["models"][0]
        assert model["verified_capabilities"] == []
        assert model["verification"][0]["state"] == "failure"
        assert reader.store.records({})[0]["usage_known"]
    assert runtime.store.db.execute("SELECT state FROM verifications").fetchone()[0] == "success"


@pytest.mark.parametrize(
    "policy,code,streaming,with_completion_count",
    [
        ({"daily_token_limit": 100000}, "quota_usage_unknown", False, False),
        ({"daily_token_limit": 100000}, "quota_usage_unknown", True, True),
        ({"daily_budget": "1"}, "budget_cost_unknown", False, True),
        ({"daily_budget": "1"}, "budget_cost_unknown", True, False),
    ],
)
def test_contradictory_reasoning_retains_reservation_and_blocks_new_admission(
    runtime, streaming, with_completion_count, policy, code
):
    caller = runtime.store.create_token("reasoning", policy=policy)
    price(runtime)
    counts = {
        "prompt_tokens": 1,
        "total_tokens": 2,
        "completion_tokens_details": {"reasoning_tokens": 5000},
    }
    if with_completion_count:
        counts["completion_tokens"] = 1
    calls = []

    def upstream(request):
        calls.append(request)
        if streaming:
            return httpx.Response(
                200,
                content=sse(event("OK"), event(None, "stop", counts), "[DONE]"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=completion() | {"usage": counts})

    with TestClient(app_for(runtime, upstream)) as client:
        response = client.post(
            "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": streaming}
        )
        assert response.status_code == 200
        row = runtime.store.records({})[0]
        assert row["reported_usage"] == counts
        assert row["usage_consistency"] == "inconsistent" and not row["usage_known"]
        assert row["total_tokens"] is None
        assert row["resources"]["token_basis"] == "unknown_hold"
        assert row["resources"]["quota_tokens"] >= 5001
        assert row["resources"]["estimated_cost"] is None
        assert row["resources"]["cost_status"] == "unknown"
        blocked = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert blocked.status_code == 429 and blocked.json()["error"]["code"] == code
    assert len(calls) == 1
    assert runtime.store.summary({})["summary"]["unknown_consumption_requests"] == 1


@pytest.mark.parametrize(
    "policy,code",
    [
        ({"daily_token_limit": 100000}, "quota_usage_unknown"),
        ({"daily_budget": "1"}, "budget_cost_unknown"),
    ],
)
def test_legacy_reasoning_accounting_is_revalidated_without_rewriting_history(
    runtime, policy, code
):
    caller = runtime.store.create_token("legacy-reasoning", policy=policy)
    counts = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    allocation(runtime, caller, "legacy-reasoning", quote=price(runtime))
    runtime.store.finish(
        "legacy-reasoning",
        state=finished(caller, "legacy-reasoning", usage=counts),
        http_status=200,
        duration_ms=1,
    )
    # Reconstruct a contradictory response settled before reasoning bounds existed.
    counts["completion_tokens_details"] = {"reasoning_tokens": 5000}
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE requests SET usage_json=? WHERE request_id='legacy-reasoning'",
            (json.dumps(counts),),
        )
    with Runtime(runtime.directory, readonly=True) as reader:
        row = reader.store.records({})[0]
        assert row["usage_consistency"] == "inconsistent" and row["total_tokens"] is None
        assert row["resources"]["quota_tokens"] >= 5001
        assert row["resources"]["estimated_cost"] is None
        assert reader.store.quota(caller["id"])["unknown_token_requests"] == 1
    with TestClient(app_for(runtime, lambda _: pytest.fail("must not dispatch"))) as client:
        blocked = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert blocked.status_code == 429 and blocked.json()["error"]["code"] == code
    assert (
        runtime.store.db.execute(
            "SELECT quota_tokens FROM allocations WHERE request_id='legacy-reasoning'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize(
    "counts,inconsistent,floor",
    [
        (
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            False,
            2,
        ),
        ({"prompt_tokens": 1, "total_tokens": 2}, False, 2),
        ({"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}, True, 2),
        ({"prompt_tokens": 1, "total_tokens": 1}, True, 2),
        ({}, False, 1),
    ],
)
def test_reasoning_is_part_of_output_without_double_counting(counts, inconsistent, floor):
    counts = counts | {"completion_tokens_details": {"reasoning_tokens": 1}}
    assert inconsistent_usage(counts) == inconsistent
    assert observed_token_floor(counts, "openai") == floor


def test_reasoning_floor_is_bounded_and_survives_responses_normalization():
    counts = response_usage(
        {
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "output_tokens_details": {"reasoning_tokens": MAX_COUNT},
            }
        }
    )
    assert counts["completion_tokens_details"]["reasoning_tokens"] == MAX_COUNT
    assert inconsistent_usage(counts)
    assert observed_token_floor(counts, "openai") == MAX_COUNT


@pytest.mark.parametrize(
    "host,url,offline",
    [
        ("127.0.0.1", "http://127.0.0.1:8787", False),
        ("localhost", "http://localhost:8787", False),
        ("::1", "http://[::1]:8787", False),
        ("::1", "http://[::1]:8787", True),
    ],
)
def test_dashboard_health_uses_configured_loopback_host(runtime, monkeypatch, offline, host, url):
    config = runtime.settings().model_copy(update={"host": host})
    _write_private(runtime.config_path, server_document(config))
    called = []

    def health(address):
        called.append(address)
        return {"state": "running", "reachable": True, "capacity": {"active": 2}}

    monkeypatch.setattr(data, "health_status", health)
    snapshot = data.snapshot(runtime.directory, data.query_filters({}), offline=offline)
    assert called == ([] if offline else [url])
    assert snapshot["service"]["state"] == ("not_checked" if offline else "running")
    assert snapshot["service"]["capacity"] == ({} if offline else {"active": 2})
