"""Synthetic subscription authorization with function-scoped cleanup."""

import time

import pytest
from pydantic import SecretStr

from selark.chatgpt_auth import ISSUER, SCOPES, ChatGPTAuth, Profile
from selark.config import initialize
from selark.runtime import Runtime

PAYLOAD = {"connection_id": "personal", "messages": [{"role": "user", "content": "hello"}]}


POLICY = {
    "allow_chatgpt_subscriptions": True,
    "allowed_connections": ["personal"],
    "chatgpt_output_reservation": 128,
    "daily_token_limit": 10000,
}


@pytest.fixture
def resource(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    initialize(tmp_path, "fake-existing-key", models=["mimo-test"], default_model="mimo-test")
    with Runtime(tmp_path) as runtime:
        runtime.add_connection("personal", "chatgpt", None, allowed_models=["gpt-test"])
        auth = ChatGPTAuth(tmp_path)
        auth.write(
            Profile(
                connection_id="personal",
                client_id="fake-issued-client",
                issuer=ISSUER,
                subject="fake-subject",
                generation=1,
                state="connected",
                scopes=SCOPES.split(),
                access_token=SecretStr("fake-own-access"),
                refresh_token=SecretStr("fake-own-refresh"),
                expires_at=time.time() + 3600,
            )
        )
        caller = runtime.store.create_token("personal client", policy=POLICY)
        yield runtime, caller, auth
