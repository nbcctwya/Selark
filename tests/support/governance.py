"""Shared resource, application and ledger fixtures for governance regressions."""

import httpx
import pytest

from selark.app import create_app
from selark.config import initialize
from selark.governance import Price
from selark.runtime import Runtime
from selark.storage import utc_now

from .protocol import UPSTREAM_KEY, completion

PAYLOAD = {"messages": [{"role": "user", "content": "test"}], "max_tokens": 16}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "config"
    initialize(directory, UPSTREAM_KEY, models=["mimo-test", "other"], default_model="mimo-test")
    with Runtime(directory) as runtime:
        yield runtime


def auth(caller):
    return {"Authorization": "Bearer " + caller["token"]}


def app_for(runtime, handler):
    return create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(handler))


def price(runtime, **values):
    document = {
        "connection_id": "mimo-default",
        "model": "mimo-test",
        "currency": "USD",
        "input_per_million": "1",
        "output_per_million": "2",
        **values,
    }
    runtime.edit_resource("price", (document["connection_id"], document["model"]), document)
    return Price.model_validate(document)


def allocation(runtime, caller, identity, *, tokens=400, quote=None):
    runtime.store.begin_request(identity, utc_now(), caller["id"], "mimo-default", "chat")
    runtime.store.attempt(
        identity,
        "mimo-test",
        None,
        "mimo-default",
        resource={
            "caller": caller["id"],
            "provider": "mimo",
            "estimate": tokens - 16,
            "output": 16,
            "price": quote,
            "alias": None,
        },
    )


def finished(caller, identity, **changes):
    return {
        "request_id": identity,
        "caller_id": caller["id"],
        "started_at": utc_now(),
        "connection_id": "mimo-default",
        "purpose": "chat",
        "model": "mimo-test",
        "actual_model": "mimo-test",
        "upstream_attempts": 1,
        "upstream_result": "success",
        "usage_final": True,
        "usage": completion()["usage"],
        "delivery_status": "sent",
        **changes,
    }
