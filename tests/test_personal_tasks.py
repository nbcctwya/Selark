"""Explicit personal-task scope; isolated credentials and mock providers only."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.cli import main
from selark.runtime import fingerprint
from selark.storage import utc_now

from .conftest import event
from .test_governance import app_for, auth
from .test_governance import runtime as runtime
from .test_service import sse
from .test_token_plans import MODEL, PAYLOAD, PLAN, caller, handler, quota, reply
from .test_token_plans import plan as plan


def allow_tasks(runtime, plan):
    runtime.change_connection(
        {"plan": plan.plan.model_dump() | {"use": "personal_interactive"}}, PLAN
    )


@pytest.mark.parametrize("stream", [False, True])
def test_personal_tasks_require_explicit_opt_in_and_keep_old_coding(runtime, plan, stream):
    token = caller(runtime)
    body = PAYLOAD | {"interaction": "personal_interactive", "stream": stream}
    calls = []
    with TestClient(app_for(runtime, handler(calls))) as client:
        denied = client.post("/v1/chat/completions", headers=auth(token), json=body)
        assert denied.status_code == 403 and not calls
    allow_tasks(runtime, plan)
    response = (
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                event("hello") | {"model": MODEL},
                event(None, "stop", reply()["usage"]) | {"model": MODEL},
                "[DONE]",
            ),
        )
        if stream
        else httpx.Response(200, json=reply())
    )
    with TestClient(app_for(runtime, handler(calls, response=response))) as client:
        result = client.post("/v1/chat/completions", headers=auth(token), json=body)
        assert result.status_code == 200
        if stream:
            assert "[DONE]" in result.text
    row = runtime.store.records({"purpose": "interactive_task"})[0]
    assert row["status"] == "success" and row["usage_known"]
    assert row["resources"]["plan_snapshot"]["use"] == "personal_interactive"
    upstream = next(r for r in calls if r.method == "POST")
    assert "interaction" not in json.loads(upstream.content)
    assert "personal interactive use" in upstream.headers["user-agent"]
    # The old spelling and purpose remain valid on explicitly expanded connections.
    with TestClient(app_for(runtime, handler([]))) as client:
        result = client.post("/v1/chat/completions", headers=auth(token), json=PAYLOAD)
        assert result.status_code == 200
    assert runtime.store.records({"purpose": "interactive_coding"})[0]["status"] == "success"


def test_expanded_scope_does_not_grant_ordinary_tokens_plan_access(runtime, plan):
    allow_tasks(runtime, plan)
    ordinary = runtime.store.create_token("ordinary")
    personal = caller(runtime)
    with TestClient(app_for(runtime, lambda _: pytest.fail("must reject before provider"))) as c:
        result = c.post(
            "/v1/chat/completions",
            headers=auth(ordinary),
            json=PAYLOAD | {"interaction": "personal_interactive"},
        )
        assert result.status_code == 403
        for value in (None, "batch"):
            result = c.post(
                "/v1/chat/completions",
                headers=auth(personal),
                json=PAYLOAD | {"interaction": value},
            )
            assert result.status_code == (403 if value is None else 400)


def test_scope_narrowed_during_quota_read_cannot_generate(runtime, plan):
    allow_tasks(runtime, plan)
    token = caller(runtime)
    calls = []

    def upstream(request):
        calls.append(request.method)
        assert request.method == "GET"
        runtime.change_connection({"plan": plan.plan.model_dump()}, PLAN)
        return httpx.Response(200, json=quota())

    with TestClient(app_for(runtime, upstream)) as c:
        result = c.post(
            "/v1/chat/completions",
            headers=auth(token),
            json=PAYLOAD | {"interaction": "personal_interactive"},
        )
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "plan_config_changed"
        assert c.get("/health").json()["capacity"]["active"] == 0
    assert calls == ["GET"]
    assert runtime.store.records({})[0]["upstream_attempts"] == 0


def test_interrupted_task_keeps_its_purpose_before_final_settlement(runtime, plan):
    token = caller(runtime)
    runtime.store.begin_request("task-interrupted", utc_now(), token["id"], PLAN, "chat")
    runtime.store.attempt(
        "task-interrupted",
        MODEL,
        "session",
        PLAN,
        purpose="interactive_task",
        resource={
            "caller": token["id"],
            "provider": "kimi-code",
            "estimate": 100,
            "output": 32,
            "price": None,
            "alias": None,
            "resource_type": "token_plan",
            "plan": plan.plan.model_dump() | {"use": "personal_interactive"},
        },
    )
    runtime.recover_records()
    row = runtime.store.records({"purpose": "interactive_task"})[0]
    assert row["status"] == "interrupted" and row["total_tokens"] is None
    assert row["resources"]["token_basis"] == "unknown_hold"


def test_cli_explicit_task_flag_requires_terminal_and_sets_correct_payload(
    runtime, plan, monkeypatch, capsys
):
    prefix = ["--config-dir", str(runtime.directory)]
    before = fingerprint(runtime.connection_settings()[-1])
    assert main([*prefix, "plans", "set", PLAN, "--use", "personal_interactive", "--json"]) == 0
    assert fingerprint(runtime.connection_settings()[-1]) != before
    assert "fake-plan-key" not in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main([*prefix, "ask", "Translate a sentence", "--interactive"]) == 1
    assert "交互终端" in capsys.readouterr().err
    sent = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "selark.client.SelarkClient.ask", lambda self, payload, **kw: sent.append(payload)
    )
    assert main([*prefix, "ask", "Translate a sentence", "--interactive"]) == 0
    assert sent[0]["interaction"] == "personal_interactive"
    with pytest.raises(SystemExit):
        main([*prefix, "ask", "text", "--interactive", "--interactive-coding"])
