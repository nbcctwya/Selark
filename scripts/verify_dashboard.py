"""Installed Dashboard TCP and optional Chromium acceptance. Temporary synthetic ledger only."""

import argparse
import hashlib
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from selark.config import initialize
from selark.governance import Price
from selark.runtime import Runtime, fingerprint


def files(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-shm", "-wal"))
    }


def seed(profile, count=140):
    """Fixtures are written only here, never by the Dashboard or into a user profile."""
    initialize(profile, "fixture-only-key", models=["mimo-fixture"], default_model="mimo-fixture")
    with Runtime(profile) as runtime:
        runtime.add_connection(
            "deepseek",
            "deepseek",
            "fixture-only-key",
            name="DeepSeek",
            allowed_models=["deepseek-fixture"],
        )
        runtime.add_connection("coding", "kimi-code", "fixture-only-key", name="Kimi Coding Plan")
        runtime.add_connection("subscription", "chatgpt", None, name="ChatGPT Personal")
        runtime.add_connection(
            "anthropic",
            "anthropic",
            "fixture-only-key",
            name="Anthropic",
            allowed_models=["claude-fixture"],
        )
        callers = [
            runtime.store.create_token(
                name,
                policy={
                    "max_concurrent_requests": 4,
                    "allow_token_plans": True,
                    "allowed_connections": ["mimo-default", "deepseek", "coding"],
                },
            )
            for name in ("Cursor Workspace", "Personal Notebook", "Research Tools")
        ]
        configurations = runtime.connection_settings()
        now = datetime.now(UTC)
        sample_count = min(count, 140)
        for index in range(sample_count):
            config = configurations[index % 3]
            caller = callers[index % 3]
            identity = f"fixture-{index:05d}"
            started = (
                now - timedelta(hours=(sample_count - index) / sample_count * 164 + 0.01)
            ).isoformat(timespec="microseconds")
            input_count = 1000 + (index * 127) % 9000
            output_count = 100 + (index * 43) % 2100
            unknown = index % 19 == 0
            quote = (
                Price(
                    connection_id=config.connection_id,
                    model=config.default_model,
                    currency="USD",
                    input_per_million="1",
                    output_per_million="3",
                )
                if config.resource_type == "api"
                else None
            )
            runtime.store.begin_request(
                identity, started, caller["id"], config.connection_id, "chat"
            )
            runtime.store.attempt(
                identity,
                config.default_model,
                None,
                config.connection_id,
                resource={
                    "caller": caller["id"],
                    "provider": config.provider,
                    "estimate": input_count,
                    "output": output_count,
                    "price": quote,
                    "alias": None,
                    "resource_type": config.resource_type,
                },
            )
            state = {
                "request_id": identity,
                "caller_id": caller["id"],
                "started_at": started,
                "finished_at": started,
                "connection_id": config.connection_id,
                "purpose": "chat",
                "resource_type": config.resource_type,
                "model": config.default_model,
                "actual_model": config.default_model,
                "upstream_attempts": 1,
                "upstream_result": "failed" if unknown else "success",
                "delivery_status": "sent",
                "usage_final": not unknown,
                "usage": None
                if unknown
                else {
                    "prompt_tokens": input_count,
                    "completion_tokens": output_count,
                    "total_tokens": input_count + output_count,
                },
                "error_code": "fixture_timeout" if unknown else None,
                "error_message": "fixture-private-error-never-expose",
                "fingerprint": fingerprint(config),
                "queue_ms": index % 9 * 15,
            }
            runtime.store.finish(
                identity,
                state=state,
                http_status=504 if unknown else 200,
                duration_ms=150 + index % 37 * 83,
            )
        # Scale the performance fixture in one transaction, preserving allocation semantics.
        if count > sample_count:
            with runtime.store.transaction() as db:
                request_samples = [dict(r) for r in db.execute("SELECT * FROM requests")]
                allocations = {
                    r["request_id"]: dict(r) for r in db.execute("SELECT * FROM allocations")
                }
                for index in range(sample_count, count):
                    row = request_samples[index % sample_count].copy()
                    allocation_row = allocations[row["request_id"]].copy()
                    identity = f"fixture-{index:05d}"
                    row.update(request_id=identity, original_request_id=identity)
                    allocation_row["request_id"] = identity
                    for table, values in (("requests", row), ("allocations", allocation_row)):
                        columns = ",".join(values)
                        placeholders = ",".join("?" for _ in values)
                        db.execute(
                            f"INSERT INTO {table}({columns}) VALUES({placeholders})",
                            list(values.values()),
                        )
        runtime.store.create_token("Disabled integration")
        runtime.store.revoke(runtime.store.tokens()[-1]["id"])
        runtime.backup("dashboard-fixture")
        log_dir = runtime.data_dir / "logs"
        log_dir.mkdir(mode=0o700)
        log = log_dir / "selark.log"
        log.write_text(
            json.dumps(
                {
                    "timestamp": now.isoformat(),
                    "path": "/v1/chat/completions",
                    "status": 200,
                    "duration_ms": 815,
                    "api_key": "fixture-only-key",
                }
            )
            + "\nprivate unstructured line\n"
        )
        log.chmod(0o600)
        return callers[0]["id"]


def add_api_with_discovery(command, profile, env):
    """Exercise the installed onboarding command against a loopback model-list fixture."""
    calls = []

    class Models(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.command, self.path, self.headers.get("Authorization")))
            body = json.dumps(
                {"data": [{"id": "openai-fixture"}, {"id": "openai-fixture-small"}]}
            ).encode()
            self.send_response(200 if self.path == "/v1/models" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Models) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = subprocess.run(
                [
                    command,
                    "--config-dir",
                    str(profile),
                    "connection",
                    "add",
                    "openai",
                    "--provider",
                    "openai",
                    "--name",
                    "OpenAI",
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "--api-key-env",
                    "DASHBOARD_FIXTURE_KEY",
                    "--json",
                ],
                env=env
                | {
                    "DASHBOARD_FIXTURE_KEY": "fixture-only-key",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                },
                cwd=profile.parent,
                capture_output=True,
                text=True,
                timeout=40,
            )
            assert response.returncode == 0
            result = json.loads(response.stdout)
            assert result["allowed_models"] == ["openai-fixture", "openai-fixture-small"]
            assert result["discovery"]["state"] == "success"
            assert result["verification"]["state"] == "never"
            assert "fixture-only-key" not in response.stdout + response.stderr
            assert calls == [("GET", "/v1/models", "Bearer fixture-only-key")]
        finally:
            server.shutdown()
            thread.join(timeout=5)
    return {"model_list_get_requests": 1, "models_added": 2, "generation_requests": 0}


def browser_checks(url, output, first_caller):
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1050}, locale="zh-CN", color_scheme="light"
        )
        page = context.new_page()
        errors = []
        requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        page.on("request", lambda request: requests.append(request.url))
        page.goto(url)
        expect(page.locator(".metrics")).to_be_visible()
        expect(page.locator("#freshness")).to_contain_text("快照")
        assert not urlsplit(page.url).fragment
        assert not page.evaluate("document.cookie")
        assert page.locator(".chart-bar").count() > 0
        assert page.evaluate("money('0.000016000')") == "0.000016"
        expect(page.locator(".resource-row")).to_have_count(6)
        page.locator('.resource-list [data-connection="openai"]').click()
        expect(page.locator("#detail-content")).to_contain_text("openai-fixture-small")
        page.keyboard.press("Escape")
        page.screenshot(path=str(output / "overview-light.png"), full_page=True)
        page.locator('[data-view="resources"]').click()
        expect(page.locator(".connection-card")).to_have_count(6)
        page.locator('[data-connection="coding"]').click()
        expect(page.locator("#details")).to_be_visible()
        expect(page.locator("#detail-content")).to_contain_text("当前剩余 Token")
        page.keyboard.press("Escape")
        expect(page.locator("#details")).not_to_be_visible()
        page.locator('[data-view="usage"]').click()
        page.locator("#caller_id").select_option(first_caller)
        expect(page.locator("#refresh")).to_be_enabled()
        expect(page.locator("#content")).to_contain_text("Cursor Workspace")
        for metric in ("requests", "cost", "success", "latency", "tokens"):
            page.locator(f'[data-metric="{metric}"]').click()
            expect(page.locator(f'[data-metric="{metric}"]')).to_have_attribute(
                "aria-pressed", "true"
            )
        page.locator("#period").select_option("custom")
        page.locator("#since").fill("2000-01-01T00:00")
        page.locator("#until").fill("2000-01-02T00:00")
        page.locator("#apply-range").click()
        expect(page.locator("#content")).to_contain_text("这个时段还没有调用记录")
        page.locator("#reset-filters").click()
        expect(page.locator(".chart-bar").first).to_be_visible()
        page.locator("#next").click()
        expect(page.locator(".page-count")).to_contain_text("26–50")
        page.locator('[data-record="0"]').click()
        expect(page.locator("#details")).to_be_visible()
        assert "fixture-private-error-never-expose" not in page.locator("#details").inner_text()
        page.locator("#close-details").click()
        page.locator('[data-view="applications"]').click()
        expect(page.locator(".application-card")).to_have_count(5)
        page.locator("[data-application]").first.click()
        expect(page.locator("#detail-content")).to_contain_text("允许模型")
        page.keyboard.press("Escape")
        page.locator('[data-view="monitor"]').click()
        expect(page.locator("#content")).to_contain_text("完整性未检查")
        expect(page.locator(".log-line")).to_have_count(1)
        page.screenshot(path=str(output / "monitor-light.png"), full_page=True)
        page.locator('[data-view="overview"]').click()
        page.locator("#theme").click()  # system -> light
        page.locator("#theme").click()  # light -> dark
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.screenshot(path=str(output / "overview-dark.png"), full_page=True)
        page.reload()
        expect(page.locator(".metrics")).to_be_visible()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.set_viewport_size({"width": 390, "height": 844})
        for view in ("overview", "resources", "usage", "applications", "monitor"):
            page.locator(f'[data-view="{view}"]').click()
            expect(page.locator("#refresh")).to_be_enabled()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), view
        page.locator('[data-view="overview"]').click()
        expect(page.locator('[data-view="overview"]')).to_have_class("nav-item active")
        expect(page.locator("#refresh")).to_be_enabled()
        page.screenshot(path=str(output / "overview-mobile.png"), full_page=True)
        assert not errors, errors
        assert all(urlsplit(r).netloc == urlsplit(url).netloc for r in requests)
        isolated = browser.new_context()
        unauth = isolated.new_page()
        unauth.goto(url.split("#")[0])
        expect(unauth.locator("#content")).to_contain_text("需要本机管理会话")
        browser.close()
    return {
        "browser": "Chromium",
        "console_errors": len(errors),
        "external_requests": 0,
        "responsive_widths": [1440, 390],
        "views_checked": 5,
    }


def run(command, *, browser_output=None, rows=140):
    checks = []
    report = {"fixture_only": True, "real_provider_requests": 0, "synthetic_ledger_rows": rows}
    with tempfile.TemporaryDirectory(prefix="selark-dashboard-") as temporary:
        root = Path(temporary)
        profile = root / "profile"
        # Avoid a caller's data override when constructing isolated fixtures.
        old_override = os.environ.pop("SELARK_DATA_DIR", None)
        try:
            first_caller = seed(profile, rows)
        finally:
            if old_override is not None:
                os.environ["SELARK_DATA_DIR"] = old_override
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("SELARK_") and k != "PYTHONPATH"
        }
        report["automatic_discovery"] = add_api_with_discovery(command, profile, env)
        checks.append("installed_cli_without_models_automatically_discovers")
        before = files(profile)
        process = subprocess.Popen(
            [
                command,
                "--config-dir",
                str(profile),
                "dashboard",
                "--port",
                "0",
                "--no-open",
                "--offline",
            ],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = queue.Queue()

        def read_output():
            for line in process.stdout:
                if line.startswith("http://127.0.0.1:"):
                    output.put(line.strip())

        thread = threading.Thread(target=read_output, daemon=True)
        thread.start()
        try:
            url = output.get(timeout=20)
            origin = url.split("/#")[0]
            report["host"] = urlsplit(origin).hostname
            with httpx.Client(base_url=origin, trust_env=False, timeout=30) as client:
                for path in ("/", "/assets/app.js", "/assets/style.css", "/assets/mark.svg"):
                    assert client.get(path).status_code == 200
                checks.append("installed_assets_outside_repository")
                assert client.get("/api/snapshot").status_code == 401
                assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
                assert client.post("/v1/chat/completions").status_code == 405
                checks.append("unauthenticated_rebinding_and_mutation_rejected")
                if browser_output:
                    report["browser"] = browser_checks(url, browser_output, first_caller)
                    checks.append("browser_desktop_mobile_theme_filters_details_and_session")
                else:
                    auth = client.post(
                        "/api/session",
                        json={"token": urlsplit(url).fragment.removeprefix("login=")},
                        headers={"Origin": origin},
                    )
                    assert auth.status_code == 200
                    headers = {"X-Selark-Dashboard": "1"}
                    started = time.perf_counter()
                    result = client.get("/api/snapshot", headers=headers)
                    report["snapshot_seconds"] = round(time.perf_counter() - started, 3)
                    assert result.status_code == 200
                    assert result.json()["usage"]["summary"]["requests"] == rows
                    for endpoint in ("records", "logs", "backups"):
                        response = client.get("/api/" + endpoint, headers=headers)
                        assert response.status_code == 200
                        assert "fixture-only-key" not in response.text
                        assert "fixture-private-error-never-expose" not in response.text
                    assert (
                        client.get(
                            "/api/snapshot", headers=headers | {"Origin": "https://evil.example"}
                        ).status_code
                        == 403
                    )
                    checks.append("authenticated_queries_and_redaction")
                assert files(profile) == before
                checks.append("configuration_ledger_oauth_and_backup_content_unchanged")
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        # Uvicorn re-raises SIGINT after graceful shutdown; the existing CLI maps it to 130.
        assert process.returncode in {0, 130}, process.returncode
        report["shutdown_exit_code"] = process.returncode
        checks.append("clean_shutdown")
    return report | {"passed": True, "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selark", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--browser-output", type=Path)
    parser.add_argument("--rows", type=int, default=140)
    args = parser.parse_args()
    result = run(str(args.selark.resolve()), browser_output=args.browser_output, rows=args.rows)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
