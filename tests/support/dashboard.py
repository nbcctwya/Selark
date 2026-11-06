"""Synthetic Dashboard sessions; callers own the TestClient context."""

from fastapi.testclient import TestClient

from selark.dashboard import create_dashboard

ORIGIN = "http://127.0.0.1:8790"


BOOTSTRAP = "test-dashboard-bootstrap-not-a-caller-token"


HEADERS = {"X-Selark-Dashboard": "1"}


def dashboard(directory, **options):
    return TestClient(
        create_dashboard(directory, port=8790, bootstrap=BOOTSTRAP, **options),
        base_url=ORIGIN,
    )


def login(client):
    result = client.post("/api/session", json={"token": BOOTSTRAP}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    client.headers["X-Selark-Session"] = result.json()["session_token"]
    return result
