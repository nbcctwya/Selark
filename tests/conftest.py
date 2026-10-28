import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.config import ServerConfig

TOKEN = "unit-test-selark-token-0000000000000000"
UPSTREAM_KEY = "unit-test-upstream-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def completion(usage=True):
    data = {
        "id": "test-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "mimo-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "你好"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage:
        data["usage"] = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}
    return data


def event(content="你好", finish=None, usage=None):
    return {
        "id": "test-stream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mimo-test",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
        "usage": usage,
    }


@pytest.fixture
def server_config():
    return ServerConfig(
        api_key=UPSTREAM_KEY,
        access_token=TOKEN,
        allowed_models=["mimo-test", "mimo-other"],
        default_model="mimo-test",
    )


@pytest.fixture
def gateway(server_config):
    clients = []

    def factory(handler=None):
        calls = []

        def capture(request):
            calls.append(request)
            return handler(request) if handler else httpx.Response(200, json=completion())

        client = TestClient(create_app(server_config, transport=httpx.MockTransport(capture)))
        clients.append(client)
        client.__enter__()
        return client, calls

    yield factory
    for client in reversed(clients):
        client.__exit__(None, None, None)
