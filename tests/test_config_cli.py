import json
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from selark.cli import main
from selark.client import ClientError, SelarkClient
from selark.config import (
    ClientConfig,
    ConfigError,
    ServerConfig,
    config_directory,
    initialize,
    load_config,
)
from selark.sse import SSEDecoder

from .conftest import TOKEN, UPSTREAM_KEY, completion, event
from .test_service import sse


@pytest.mark.parametrize("xdg", [None, "", "relative-config"])
def test_default_config_is_independent_of_cwd(tmp_path, monkeypatch, xdg):
    fake_home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("SELARK_CONFIG_DIR", raising=False)
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    for name in ("terminal-one", "terminal-two"):
        working = tmp_path / name
        working.mkdir()
        monkeypatch.chdir(working)
        assert config_directory() == fake_home / ".config" / "selark"


def test_absolute_config_overrides_and_priority(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SELARK_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_directory() == tmp_path / "xdg" / "selark"
    monkeypatch.setenv("SELARK_CONFIG_DIR", str(tmp_path / "service"))
    assert config_directory() == tmp_path / "service"
    monkeypatch.setenv("SELARK_CONFIG_DIR", "invalid-relative")
    assert config_directory(tmp_path / "explicit") == tmp_path / "explicit"


@pytest.mark.parametrize("command", ["init", "serve", "models", "ask"])
def test_relative_config_is_rejected_without_traceback(tmp_path, monkeypatch, capsys, command):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SELARK_CONFIG_DIR", "relative-config")
    assert main([command]) == 1
    monkeypatch.delenv("SELARK_CONFIG_DIR")
    assert main(["--config-dir", "relative-config", command]) == 1
    output = capsys.readouterr()
    assert "绝对路径" in output.err
    assert "Traceback" not in output.err
    assert not (tmp_path / "relative-config").exists()


def test_help_does_not_require_valid_configuration(monkeypatch, capsys):
    monkeypatch.setenv("SELARK_CONFIG_DIR", "invalid-relative")
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert "serve" in capsys.readouterr().out


def test_init_and_commands_reuse_default_configuration_after_chdir(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("SELARK_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("MIMO_API_KEY", UPSTREAM_KEY)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    assert main(["init"]) == 0
    directory = fake_home / ".config" / "selark"
    original = (directory / "server.json").read_bytes()
    monkeypatch.chdir(second)
    monkeypatch.delenv("MIMO_API_KEY")
    assert main(["init"]) == 1
    assert (directory / "server.json").read_bytes() == original

    servers = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: servers.append(kwargs))
    assert main(["serve"]) == 0
    assert servers[0]["host"] == "127.0.0.1" and servers[0]["port"] == 8787

    client_config = load_config(directory / "client.json", ClientConfig)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.headers["Authorization"] == (
            "Bearer " + client_config.access_token.get_secret_value()
        )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test"}]})
        return httpx.Response(200, json=completion())

    real_http = httpx.Client
    monkeypatch.setattr(
        "selark.client.httpx.Client",
        lambda **kw: real_http(**(kw | {"transport": httpx.MockTransport(handler)})),
    )
    assert main(["models"]) == 0
    assert main(["ask", "hi"]) == 0
    assert len(calls) == 2
    assert not (first / "server.json").exists() and not (second / "server.json").exists()
    output = capsys.readouterr()
    assert UPSTREAM_KEY not in output.out + output.err


def test_init_persistence_permissions_separation_and_no_overwrite(tmp_path):
    directory = tmp_path / "private"
    initialize(directory, UPSTREAM_KEY, models=["test"], default_model="test", port=9898)
    server = load_config(directory / "server.json", ServerConfig)
    client = load_config(directory / "client.json", ClientConfig)
    assert server.api_key.get_secret_value() == UPSTREAM_KEY
    assert server.access_token == client.access_token
    assert server.access_token.get_secret_value() != UPSTREAM_KEY
    assert len(server.access_token.get_secret_value()) >= 32
    assert client.url == "http://127.0.0.1:9898"
    assert client.model == server.default_model == "test"
    assert UPSTREAM_KEY not in (directory / "client.json").read_text()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("server.json", "client.json"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    original = (directory / "server.json").read_bytes()
    with pytest.raises(ConfigError, match="已存在"):
        initialize(directory, "replacement")
    assert (directory / "server.json").read_bytes() == original
    # A new load has no dependency on in-memory initialization state.
    assert load_config(directory / "server.json", ServerConfig) == server


@pytest.mark.parametrize(
    "options",
    [
        {"base_url": "http://example.com/v1"},
        {"base_url": "https://user:password@example.com/v1"},
        {"base_url": "https://example.com/v1?key=secret"},
        {"base_url": "https://example.com/#secret"},
        {"models": ["one"], "default_model": "two"},
        {"models": ["one", "one"], "default_model": "one"},
        {"port": 0},
        {"port": 65536},
    ],
)
def test_bad_config_never_writes_secret(tmp_path, options):
    with pytest.raises(ConfigError) as error:
        initialize(tmp_path / "bad", UPSTREAM_KEY, **options)
    assert UPSTREAM_KEY not in str(error.value)
    assert not (tmp_path / "bad" / "server.json").exists()


def test_invalid_private_file_does_not_leak_raw_data(tmp_path):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"url": UPSTREAM_KEY, "access_token": TOKEN, "model": "x"}))
    path.chmod(0o600)
    with pytest.raises(ConfigError) as error:
        load_config(path, ClientConfig)
    assert UPSTREAM_KEY not in str(error.value)
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(path, ClientConfig)
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(ConfigError, match="符号链接"):
        load_config(alias, ClientConfig)


def test_cli_init_no_output_of_secrets_and_models_uses_only_client_config(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("MIMO_API_KEY", UPSTREAM_KEY)
    assert main(["--config-dir", str(tmp_path), "init"]) == 0
    output = capsys.readouterr()
    assert UPSTREAM_KEY not in output.out + output.err
    assert main(["--config-dir", str(tmp_path), "init"]) == 1
    (tmp_path / "server.json").unlink()
    config = load_config(tmp_path / "client.json", ClientConfig)
    calls = []

    def handler(request):
        calls.append(request)
        assert (
            request.headers["Authorization"] == "Bearer " + config.access_token.get_secret_value()
        )
        assert "api-key" not in request.headers
        return httpx.Response(200, json={"object": "list", "data": [{"id": "test"}]})

    real_http = httpx.Client
    monkeypatch.setattr(
        "selark.client.httpx.Client",
        lambda **kw: real_http(**(kw | {"transport": httpx.MockTransport(handler)})),
    )
    assert main(["--config-dir", str(tmp_path), "models"]) == 0
    assert calls[0].url.host == "127.0.0.1"
    assert "test" in capsys.readouterr().out


def test_cli_connection_failure_no_provider_fallback(tmp_path, monkeypatch, capsys):
    initialize(tmp_path, UPSTREAM_KEY)
    (tmp_path / "server.json").unlink()
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    real_http = httpx.Client
    monkeypatch.setattr(
        "selark.client.httpx.Client",
        lambda **kw: real_http(**(kw | {"transport": httpx.MockTransport(handler)})),
    )
    assert main(["--config-dir", str(tmp_path), "ask", "hi"]) == 1
    output = capsys.readouterr()
    assert "无法连接 Selark" in output.err
    assert len(calls) == 1
    assert calls[0].url.host == "127.0.0.1"
    assert UPSTREAM_KEY not in output.out + output.err


def test_cli_regular_and_stream_outputs(capsys):
    config = ClientConfig(url="http://127.0.0.1:8787", access_token=TOKEN, model="mimo-test")

    def handler(request):
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200,
                content=sse(event(), event(None, "stop"), "[DONE]"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=completion())

    with SelarkClient(config, transport=httpx.MockTransport(handler)) as client:
        client.ask({"stream": False})
        output = capsys.readouterr()
        assert output.out == "你好\n"
        assert json.loads(output.err)["usage"] == completion()["usage"]
        client.ask({"stream": True})
        output = capsys.readouterr()
        assert output.out == "你好\n"
        assert json.loads(output.err)["usage"] is None


@pytest.mark.parametrize(
    "body",
    [
        sse(event()),
        sse({"error": {"message": "upstream failed"}}),
        b"data: invalid\n\n",
    ],
)
def test_cli_fails_on_partial_or_error_stream(body):
    config = ClientConfig(access_token=TOKEN)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})
    )
    with SelarkClient(config, transport=transport) as client, pytest.raises(ClientError):
        client.ask({"stream": True})


def test_client_redirect_is_not_followed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://invalid.example"})

    with (
        SelarkClient(ClientConfig(access_token=TOKEN), transport=httpx.MockTransport(handler)) as c,
        pytest.raises(ClientError, match="307"),
    ):
        c.models()
    assert len(calls) == 1


def test_client_cli_import_does_not_import_provider():
    source = (
        "import sys; import selark.cli; "
        "assert 'selark.providers.mimo' not in sys.modules; "
        "assert 'selark.app' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-c", source], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_example_standalone_help_and_empty_input(tmp_path):
    script = Path(__file__).resolve().parents[1] / "examples" / "summarize.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--config" in result.stdout
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps({"url": "http://127.0.0.1:8787", "access_token": TOKEN, "model": "x"})
    )
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--config", str(path)],
        cwd=tmp_path,
        input="",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert TOKEN not in result.stdout + result.stderr


def test_sse_multiline_and_limits():
    decoder = SSEDecoder()
    assert decoder.feed(": heartbeat") is None
    assert decoder.feed('data: {"key":') is None
    assert decoder.feed('data: "value"}') is None
    assert json.loads(decoder.feed("")) == {"key": "value"}
    with pytest.raises(ValueError, match="过大"):
        decoder.feed("data: " + "x" * 1_048_577)
