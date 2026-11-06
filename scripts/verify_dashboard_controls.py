"""Installed React management forms and same-port restarts; synthetic configuration only."""

import argparse
import json
import os
import queue
import signal
import socket
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from selark.config import _write_private, initialize
from selark.connections import require_connection
from selark.runtime import Runtime


@contextmanager
def dashboard(command, profile, env, port=0, *, offline=True):
    process = subprocess.Popen(
        [
            command,
            "--config-dir",
            str(profile),
            "dashboard",
            "--no-open",
            *(["--offline"] if offline else []),
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


def browser_management(page, directory):
    from playwright.sync_api import expect

    calls = []

    class Models(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.command, self.path, self.headers.get("Authorization")))
            body = json.dumps({"data": [{"id": "fixture-ui-model"}]}).encode()
            self.send_response(200 if self.path == "/v1/models" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    nav = page.get_by_role("navigation", name="主导航")
    with ThreadingHTTPServer(("127.0.0.1", 0), Models) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            nav.get_by_role("button", name="连接与模型", exact=True).click()
            page.get_by_role("button", name="新建连接").click()
            create = page.get_by_role("dialog", name="新建连接")
            create.get_by_role("radio", name="通用 API（OpenAI 兼容）", exact=False).click()
            create.get_by_label("连接 ID").fill("ui-relay")
            create.get_by_label("显示名称").fill("UI Relay")
            create.get_by_label("接口地址").fill(f"http://127.0.0.1:{server.server_port}/v1")
            create.get_by_label("API Key", exact=True).fill("fixture-ui-key")
            create.get_by_role("button", name="创建连接").click()
            sheet = page.get_by_role("dialog", name="UI Relay")
            expect(sheet.get_by_text("目录中还没有模型")).to_be_visible()
            assert calls == []
            sheet.get_by_role("button", name="发现模型").click()
            expect(sheet.get_by_role("status")).to_contain_text("发现完成")
            model = sheet.get_by_role("switch", name="启用 fixture-ui-model", exact=True)
            expect(model).to_have_attribute("aria-checked", "false")
            sheet.get_by_role("button", name="启用目录全部").click()
            expect(model).to_have_attribute("aria-checked", "true")
            sheet.get_by_role("button", name="将 fixture-ui-model 设为默认模型").click()
            expect(sheet.get_by_role("status")).to_contain_text("已设为默认模型")
            sheet.get_by_role("tab", name="设置").click()
            sheet.get_by_label("显示名称").fill("UI Relay Updated")
            sheet.get_by_role("button", name="更换 Key").click()
            sheet.get_by_label("新的 API Key").fill("fixture-ui-rotated-key")
            sheet.get_by_role("button", name="保存修改").click()
            sheet = page.get_by_role("dialog", name="UI Relay Updated")
            expect(sheet.get_by_role("status")).to_contain_text("已保存")
            expect(page.locator('input[type="password"]')).to_have_count(0)
            assert "fixture-ui-rotated-key" not in page.content()
            page.keyboard.press("Escape")
            assert calls == [("GET", "/v1/models", "Bearer fixture-ui-key")]
        finally:
            server.shutdown()
            thread.join(timeout=5)

    nav.get_by_role("button", name="应用管理", exact=True).click()
    page.get_by_role("button", name="新建应用").click()
    create = page.get_by_role("dialog", name="新建应用")
    create.get_by_label("应用名称").fill("UI Editor")
    create.get_by_label("日 Token 上限").fill("100000")
    # Hold the real POST until we have checked that Esc cannot lose its one-time token.
    pending = []
    page.route("**/api/applications", lambda route: pending.append(route))
    with page.expect_request(lambda request: request.method == "POST"):
        create.get_by_role("button", name="创建并生成令牌").click()
    expect(create.get_by_role("button", name="关闭详情")).to_be_disabled()
    page.keyboard.press("Escape")
    expect(create).to_be_visible()
    assert len(pending) == 1
    pending[0].continue_()
    token = create.get_by_label("一次性调用令牌")
    expect(token).to_be_visible()
    value = token.input_value()
    assert value and value not in page.evaluate("JSON.stringify(localStorage)")
    assert value not in page.evaluate("JSON.stringify(sessionStorage)")
    page.unroute("**/api/applications")
    create.get_by_role("button", name="我已保存，关闭").click()
    expect(page.get_by_label("一次性调用令牌")).to_have_count(0)
    assert value not in page.content()
    page.get_by_role("button", name="UI Editor", exact=True).click()
    sheet = page.get_by_role("dialog", name="UI Editor")
    expect(sheet).to_contain_text("100,000")
    sheet.get_by_role("tab", name="策略与撤销").click()
    expect(sheet.get_by_label("日 Token 上限")).to_have_value("100000")
    sheet.get_by_label("并发上限").fill("2")
    sheet.get_by_role("button", name="保存策略").click()
    expect(sheet.get_by_role("status")).to_contain_text("策略已保存")
    sheet.get_by_role("button", name="撤销令牌").click()
    page.get_by_role("button", name="永久撤销").click()
    expect(sheet).to_contain_text("应用 · 已撤销")
    page.keyboard.press("Escape")
    with Runtime(directory, readonly=True) as runtime:
        connection = require_connection(runtime.connection_settings(), "ui-relay")
        assert connection.name == "UI Relay Updated"
        assert connection.api_key.get_secret_value() == "fixture-ui-rotated-key"
        assert connection.allowed_models == ["fixture-ui-model"]
        assert connection.default_model == "fixture-ui-model"
        application = next(a for a in runtime.store.tokens() if a["name"] == "UI Editor")
        assert not application["active"]
        assert application["policy"]["daily_token_limit"] == 100000
        assert application["policy"]["max_concurrent_requests"] == 2
    return {
        "connection_create_edit_key_rotation": True,
        "explicit_model_discovery": True,
        "batch_approval_and_default": True,
        "application_create_policy_quota_revoke": True,
        "one_time_token_retained_until_response": True,
        "model_list_get_requests": len(calls),
    }


def browser_controls(url, directory, output):
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    errors, requests = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, color_scheme="light")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                errors.append(message.text)
                if message.type == "error" and "409 (Conflict)" not in message.text
                else None
            ),
        )
        page.on("request", lambda request: requests.append(request.url))
        page.goto(url)
        nav = page.get_by_role("navigation", name="主导航")
        nav.get_by_role("button", name="连接与模型", exact=True).click()
        page.get_by_role("button", name="OpenAI", exact=True).click()
        sheet = page.get_by_role("dialog", name="OpenAI", exact=True)
        search = sheet.get_by_role("searchbox", name="查找模型")
        search.fill("gpt-6.1")
        expect(sheet.locator(".toolbar-count")).to_have_text("1 个模型")
        toggle = sheet.get_by_role("switch", name="启用 gpt-6.1-sol", exact=True)
        toggle.click()
        expect(toggle).to_have_attribute("aria-checked", "true")
        expect(sheet.get_by_role("status")).to_contain_text("已启用")
        page.screenshot(path=str(output / "model-controls-light.png"), full_page=True)
        toggle.click()
        expect(toggle).to_have_attribute("aria-checked", "false")
        search.fill("gpt-6-luna")
        default = sheet.get_by_role("switch", name="启用 gpt-6-luna", exact=True)
        default.click()
        confirmation = page.get_by_role("dialog", name="禁用默认模型？")
        expect(confirmation).to_be_visible()
        confirmation.get_by_role("button", name="取消").click()
        expect(confirmation).to_have_count(0)
        default.click()
        page.get_by_role("button", name="禁用并清除默认", exact=True).click()
        expect(default).to_have_attribute("aria-checked", "false")
        default.click()
        expect(default).to_have_attribute("aria-checked", "true")
        with Runtime(directory, readonly=True) as runtime:
            assert runtime.connection("openai")["default_model"] is None
            runtime.change_models("openai", "enable", ["fixture-model-0"])
        search.fill("gpt-6.1")
        toggle.click()
        expect(sheet.get_by_role("alert")).to_contain_text("已被其他操作更新")
        expect(toggle).to_have_attribute("aria-checked", "false")
        toggle.click()
        expect(toggle).to_have_attribute("aria-checked", "true")
        scope = sheet.get_by_role("radiogroup", name="启用状态")
        scope.get_by_role("radio", name="未启用", exact=True).click()
        expect(sheet.locator(".toolbar-count")).to_have_text("0 个模型")
        scope.get_by_role("radio", name="已启用", exact=True).click()
        expect(sheet.locator(".toolbar-count")).to_have_text("1 个模型")
        page.keyboard.press("Escape")
        page.get_by_role("radiogroup", name="主题").get_by_role(
            "radio", name="深色", exact=True
        ).click()
        page.get_by_role("button", name="OpenAI", exact=True).click()
        search.fill("gpt-6")
        page.screenshot(path=str(output / "model-controls-dark.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        box = toggle.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 390 and box["height"] >= 44
        toggle.click()
        expect(toggle).to_have_attribute("aria-checked", "false")
        page.screenshot(path=str(output / "model-controls-mobile.png"), full_page=True)
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 1440, "height": 1050})
        management = browser_management(page, directory)
        assert not errors, errors
        assert all(urlsplit(item).netloc == urlsplit(url).netloc for item in requests)
        browser.close()
    return {
        "javascript_errors": 0,
        "external_browser_requests": 0,
        "enable_disable_search_and_filter": True,
        "default_confirmation": True,
        "stale_revision_rejected": True,
        "mobile_and_dark_theme": True,
        **management,
    }


def run(command, browser_output=None):
    report = {"real_provider_requests": 0, "model_generation_requests": 0, "restarts": 3}
    env = {k: v for k, v in os.environ.items() if not k.startswith("SELARK_") and k != "PYTHONPATH"}
    with (
        tempfile.TemporaryDirectory(prefix="selark-dashboard-controls-") as temporary,
        socket.socket() as unused_gateway,
    ):
        directory = Path(temporary) / "profile"
        previous = os.environ.pop("SELARK_DATA_DIR", None)
        try:
            initialize(directory, "fixture-controls-key", models=["fixture-default"])
            # Reserve an unused port so online health reads cannot contact a personal gateway.
            unused_gateway.bind(("127.0.0.1", 0))
            server_path = directory / "server.json"
            document = json.loads(server_path.read_text())
            document["port"] = unused_gateway.getsockname()[1]
            _write_private(server_path, document)
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
                    dashboard(command, directory, env, port, offline=not bool(browser_output)) as (
                        _,
                        url,
                    ),
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
                            auth = client.post(
                                origin + "/api/session",
                                json={"token": bootstrap},
                                headers={"Origin": origin},
                            )
                            assert auth.status_code == 200
                            headers = {
                                "X-Selark-Dashboard": "1",
                                "Origin": origin,
                                "X-Selark-Session": auth.json()["session_token"],
                            }
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
                actual = runtime.store.tokens()
                assert [a for a in actual if a["name"] != "UI Editor"] == policies
                if browser_output:
                    created = [a for a in actual if a["name"] == "UI Editor"]
                    assert len(created) == 1 and not created[0]["active"]
                else:
                    assert runtime.store.path.read_bytes() == database
                assert len(runtime.connection("openai")["discovery"]["models"]) == 130
                assert runtime.store.records({}) == []
            report.update(
                passed=True,
                live_listener_not_taken_over=True,
                request_ledger_and_existing_policies_unchanged=True,
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
