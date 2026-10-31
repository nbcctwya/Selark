"""Installed model buttons and immediate same-port restarts; synthetic configuration only."""

import argparse
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from selark.config import initialize
from selark.connections import require_connection
from selark.runtime import Runtime


@contextmanager
def dashboard(command, profile, env, port=0):
    process = subprocess.Popen(
        [
            command,
            "--config-dir",
            str(profile),
            "dashboard",
            "--no-open",
            "--offline",
            "--port",
            str(port),
        ],
        env=env,
        cwd=profile.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = queue.Queue()

    def consume():
        for line in process.stdout:
            if line.startswith("http://127.0.0.1:"):
                ready.put(line.strip())

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    try:
        yield process, ready.get(timeout=15)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
        thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
        assert process.returncode in (0, 130)


def browser_controls(url, directory, output):
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    errors, requests = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, color_scheme="light")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(url)
        expect(page.locator("#dashboard-mode")).to_have_text("模型管理")
        page.locator('[data-view="resources"]').click()
        page.locator('[data-connection="openai"]').click()
        page.locator("#model-search").fill("gpt-6.1")
        expect(page.locator("#model-match-count")).to_have_text("1 个模型")
        page.get_by_role("button", name="启用 gpt-6.1-sol", exact=True).click()
        expect(page.locator("#model-action-status")).to_contain_text("已启用")
        expect(page.get_by_role("button", name="禁用 gpt-6.1-sol", exact=True)).to_be_enabled()
        page.screenshot(path=str(output / "model-controls-light.png"), full_page=True)
        page.get_by_role("button", name="禁用 gpt-6.1-sol", exact=True).click()
        expect(page.locator("#model-action-status")).to_contain_text("已禁用")
        page.locator("#model-search").fill("gpt-6-luna")
        page.get_by_role("button", name="禁用 gpt-6-luna", exact=True).click()
        expect(page.locator("#model-confirmation")).to_be_visible()
        page.locator("#cancel-model-change").click()
        expect(page.locator("#model-confirmation")).not_to_be_visible()
        page.get_by_role("button", name="禁用 gpt-6-luna", exact=True).click()
        page.get_by_role("button", name="禁用并清除默认", exact=True).click()
        expect(page.locator("#model-action-status")).to_contain_text("已禁用")
        page.get_by_role("button", name="启用 gpt-6-luna", exact=True).click()
        expect(page.locator("#model-action-status")).to_contain_text("已启用")
        with Runtime(directory, readonly=True) as runtime:
            assert runtime.connection("openai")["default_model"] is None
            runtime.change_models("openai", "enable", ["fixture-model-0"])
        page.locator("#model-search").fill("gpt-6.1")
        page.get_by_role("button", name="启用 gpt-6.1-sol", exact=True).click()
        expect(page.locator("#model-action-status")).to_contain_text("已被其他操作更新")
        expect(page.get_by_role("button", name="启用 gpt-6.1-sol", exact=True)).to_be_enabled()
        page.get_by_role("button", name="启用 gpt-6.1-sol", exact=True).click()
        expect(page.locator("#model-action-status")).to_contain_text("已启用")
        page.locator("#model-scope").select_option("disabled")
        expect(page.locator("#model-match-count")).to_have_text("0 个模型")
        page.locator("#model-scope").select_option("approved")
        expect(page.locator("#model-match-count")).to_have_text("1 个模型")
        page.keyboard.press("Escape")
        page.locator("#theme").click()
        page.locator("#theme").click()
        page.locator('[data-connection="openai"]').click()
        page.locator("#model-search").fill("gpt-6")
        page.screenshot(path=str(output / "model-controls-dark.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_role("button", name="禁用 gpt-6.1-sol", exact=True).click()
        expect(page.locator("#model-action-status")).to_contain_text("已禁用")
        page.screenshot(path=str(output / "model-controls-mobile.png"), full_page=True)
        assert not errors, errors
        assert all(urlsplit(item).netloc == urlsplit(url).netloc for item in requests)
        browser.close()
    return {
        "javascript_errors": 0,
        "external_requests": 0,
        "enable_disable_search_and_filter": True,
        "default_confirmation": True,
        "stale_revision_rejected": True,
        "mobile_and_dark_theme": True,
    }


def run(command, browser_output=None):
    report = {"real_provider_requests": 0, "model_generation_requests": 0, "restarts": 3}
    env = {k: v for k, v in os.environ.items() if not k.startswith("SELARK_") and k != "PYTHONPATH"}
    with tempfile.TemporaryDirectory(prefix="selark-dashboard-controls-") as temporary:
        directory = Path(temporary) / "profile"
        previous = os.environ.pop("SELARK_DATA_DIR", None)
        try:
            initialize(directory, "fixture-controls-key", models=["fixture-default"])
            with Runtime(directory) as runtime:
                runtime.add_connection(
                    "openai", "openai", "fixture-openai-key", allowed_models=["gpt-6-luna"]
                )
                runtime.save_discovery(
                    require_connection(runtime.connection_settings(), "openai"),
                    [
                        "gpt-6-luna",
                        "gpt-6.1-sol",
                        "gpt-4-fixture",
                        *[f"fixture-model-{i}" for i in range(127)],
                    ],
                )
                policies = runtime.store.tokens()
                database_path = runtime.store.path
            database = database_path.read_bytes()  # Capture after fixture WAL checkpoint/close.
            port = 0
            for cycle in range(3):
                with (
                    httpx.Client(trust_env=False, timeout=5) as client,
                    dashboard(command, directory, env, port) as (_, url),
                ):
                    port = urlsplit(url).port
                    origin = f"http://127.0.0.1:{port}"
                    assert client.get(origin).status_code == 200
                    if cycle == 0:
                        collision = subprocess.run(
                            [
                                command,
                                "--config-dir",
                                str(directory),
                                "dashboard",
                                "--port",
                                str(port),
                                "--no-open",
                            ],
                            capture_output=True,
                            text=True,
                            env=env,
                            timeout=10,
                        )
                        assert collision.returncode == 1 and "端口不可用" in collision.stderr
                        if browser_output:
                            report["browser"] = browser_controls(url, directory, browser_output)
                        else:
                            bootstrap = urlsplit(url).fragment.removeprefix("login=")
                            assert (
                                client.post(
                                    origin + "/api/session",
                                    json={"token": bootstrap},
                                    headers={"Origin": origin},
                                ).status_code
                                == 200
                            )
                            headers = {"X-Selark-Dashboard": "1", "Origin": origin}
                            state = client.get(origin + "/api/snapshot", headers=headers).json()
                            model = next(c for c in state["connections"] if c["id"] == "openai")
                            payload = {
                                "connection_id": "openai",
                                "model": "gpt-6.1-sol",
                                "enabled": True,
                                "revision": model["model_catalog"]["approval_revision"],
                            }
                            assert (
                                client.post(
                                    origin + "/api/model-approval",
                                    json=payload,
                                    headers=headers,
                                ).status_code
                                == 200
                            )
                            assert (
                                client.post(
                                    origin + "/api/model-approval",
                                    json=payload,
                                    headers=headers,
                                ).status_code
                                == 409
                            )
                    # Keep an HTTP connection open while shutting the server down;
                    # its TCP closing state must not prevent the next immediate bind.
                    assert client.get(origin).status_code == 200
            with Runtime(directory, readonly=True) as runtime:
                assert runtime.store.tokens() == policies
                assert runtime.store.path.read_bytes() == database
                assert len(runtime.connection("openai")["discovery"]["models"]) == 130
                assert runtime.store.records({}) == []
            report.update(
                passed=True, live_listener_not_taken_over=True, ledger_and_policies_unchanged=True
            )
        finally:
            if previous is not None:
                os.environ["SELARK_DATA_DIR"] = previous
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selark", required=True)
    parser.add_argument("--browser-output", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.selark, args.browser_output)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
