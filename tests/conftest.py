import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.config import ServerConfig

from .support.protocol import TOKEN, UPSTREAM_KEY, completion


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
