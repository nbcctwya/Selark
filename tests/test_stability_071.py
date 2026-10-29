"""Regressions for accounting evidence and failed connections; no provider traffic."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.cli import main
from selark.client import ClientError, SelarkClient
from selark.config import ClientConfig
from selark.errors import GatewayError
from selark.governance import usage_cost
from selark.persistence import Journal, snapshot
from selark.plans import enforce_quota, parse_quota, query_quota
from selark.providers.mimo import retry_after_seconds

from .conftest import TOKEN, completion, event
from .test_governance import PAYLOAD, allocation, app_for, auth, finished, price
from .test_governance import runtime as runtime
from .test_service import sse


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "counts",
    [
        {"prompt_tokens": 1000, "completion_tokens": 2, "total_tokens": 1},
        {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 100},
        {
            "prompt_tokens": 12,
            "completion_tokens": 2,
            "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 20},
        },
    ],
)
def test_conflicting_usage_is_traceable_and_blocks_quota(runtime, counts, streaming):
    caller = runtime.store.create_token("limited", policy={"daily_token_limit": 100000})
    quote = price(runtime)
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
        result = client.post(
            "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": streaming}
        )
        assert result.status_code == 200
        row = runtime.store.records({})[0]
        assert row["reported_usage"] == counts
        assert row["total_tokens"] is None and not row["usage_known"]
        assert row["usage_consistency"] == "inconsistent"
        assert row["resources"]["token_basis"] == "unknown_hold"
        assert row["resources"]["quota_tokens"] >= counts["prompt_tokens"]
        assert row["resources"]["estimated_cost"] is None
        blocked = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert blocked.json()["error"]["code"] == "quota_usage_unknown"
    assert len(calls) == 1
    summary = runtime.store.summary({})["summary"]
    assert summary["unknown_consumption_requests"] == 1
    assert summary["local_rejected_requests"] == 1
    assert usage_cost(quote.model_dump(), counts, "mimo") is None


def test_partial_usage_reservation_floor_survives_repeated_recovery(runtime):
    caller = runtime.store.create_token("partial")
    allocation(runtime, caller, "partial", tokens=400)
    state = finished(
        caller,
        "partial",
        usage={"prompt_tokens": 5000},
        usage_final=False,
        upstream_result="failed",
    )
    journal = Journal(runtime.data_dir / "pending")
    receipt = snapshot(state, 502, 10, settled=True)
    for _ in range(2):
        journal.save(receipt)
        assert runtime.recover_records()["restored"] == 1
        quota = runtime.store.quota(caller["id"])
        assert quota["requests"] == 1 and quota["charged_or_reserved_tokens"] == 5000
        assert quota["unknown_token_requests"] == 1
    assert runtime.store.summary({})["summary"]["unknown_consumption_requests"] == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("120", 120),
        ("9999999999", 86400),
        ("0", 0),
        ("-2", None),
        ("secret-key", None),
        ("Thu, 01 Jan 1970 00:00:00 GMT", 0),
    ],
)
def test_retry_after_parsing_is_bounded(value, expected):
    assert retry_after_seconds(value) == expected


@pytest.mark.parametrize("status", [429, 503])
def test_upstream_backoff_is_forwarded_without_retry(runtime, status):
    caller = runtime.store.create_token("backoff")
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "120"}, text="private failure")

    with TestClient(app_for(runtime, upstream)) as client:
        result = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert result.status_code == (429 if status == 429 else 502)
        assert result.headers["retry-after"] == "120"
        assert "private failure" not in result.text
    assert len(calls) == 1
    row = runtime.store.records({})[0]
    assert row["upstream_status"] == status and row["upstream_attempts"] == 1


def test_plan_quota_failure_diagnoses_preflight_and_never_generates(runtime):
    runtime.add_connection("coding", "kimi-code", "fake")
    connection = runtime.connection_settings()[1]
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "45"})

    with pytest.raises(GatewayError) as error:
        asyncio.run(query_quota(connection, transport=httpx.MockTransport(upstream)))
    assert error.value.code == "upstream_rate_limit" and error.value.retry_after == 45
    assert "未生成" in error.value.message
    assert len(calls) == 1 and calls[0].method == "GET"


def test_impossible_plan_counter_and_invalid_reset_cannot_admit():
    for raw in ({"limit": "10", "remaining": "11"}, {"remaining": "1", "resetTime": "tomorrow"}):
        quota = parse_quota({"usage": raw, "booster_wallet": {"status": "STATUS_DISABLED"}})
        with pytest.raises(GatewayError):
            enforce_quota(quota)


def test_cli_failure_links_exact_record_and_redacts_token(runtime, capsys):
    profile = ClientConfig(access_token=TOKEN)
    response = httpx.Response(
        429,
        headers={"x-request-id": "one-request", "retry-after": "12"},
        json={"error": {"message": TOKEN, "code": "upstream_rate_limit"}},
    )
    with SelarkClient(profile) as client, pytest.raises(ClientError) as error:
        client.check(response)
    assert TOKEN not in str(error.value)
    assert "records show one-request" in str(error.value) and "12" in str(error.value)
    caller = runtime.store.create_token("trace")
    allocation(runtime, caller, "one-request")
    runtime.store.finish(
        "one-request", state=finished(caller, "one-request"), http_status=200, duration_ms=1
    )
    assert (
        main(["--config-dir", str(runtime.directory), "records", "show", "one-request", "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["request_id"] == "one-request"


@pytest.mark.parametrize(
    "changed", [{"model": "another"}, {"id": "another"}, {"model": "bad\nmodel"}]
)
def test_stream_cannot_mix_reply_identities(runtime, changed):
    caller = runtime.store.create_token("mixed")
    counts = completion()["usage"]
    body = sse(event("first", usage=counts), event(None, "stop", counts) | changed, "[DONE]")
    with TestClient(
        app_for(
            runtime,
            lambda _: httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            ),
        )
    ) as client:
        result = client.post(
            "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": True}
        )
    assert "event: error" in result.text and "data: [DONE]" not in result.text
    row = runtime.store.records({})[0]
    assert row["status"] == "failed" and row["total_tokens"] is None
    assert row["resources"]["token_basis"] == "unknown_hold"


def test_regressing_stream_counters_never_release_quota(runtime):
    caller = runtime.store.create_token("regressed", policy={"daily_token_limit": 100000})
    high = {"prompt_tokens": 3000, "completion_tokens": 2000, "total_tokens": 5000}
    body = sse(event("first", usage=high), event(None, "stop", completion()["usage"]), "[DONE]")
    with TestClient(
        app_for(
            runtime,
            lambda _: httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            ),
        )
    ) as client:
        result = client.post(
            "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": True}
        )
        assert "data: [DONE]" in result.text
    row = runtime.store.records({})[0]
    assert row["reported_usage"] == completion()["usage"]
    assert row["usage_consistency"] == "regressed" and row["total_tokens"] is None
    assert row["resources"]["observed_usage_peak"] == high
    assert row["resources"]["quota_tokens"] == 5000
    assert runtime.store.quota(caller["id"])["remaining_tokens"] is None
