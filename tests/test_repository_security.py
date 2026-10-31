"""Publication checks use generated, invalid credentials and an isolated Git repository."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_repository_security.py"
spec = importlib.util.spec_from_file_location("repository_security", SCRIPT)
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)


@pytest.mark.parametrize(
    "value,rule",
    [
        (b"sk-" + b"X" * 28, "provider_key"),
        (b"ghp_" + b"X" * 36, "github_token"),
        (b"AKIA" + b"X" * 16, "aws_access_key"),
        (b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----", "private_key"),
        (b"eyJ" + b"A" * 18 + b"." + b"B" * 18 + b"." + b"C" * 18, "jwt"),
        (b"Bearer " + b"X" * 30, "bearer_literal"),
        (b"/home/" + b"private-user/config.json", "personal_home_path"),
        (b'"api_key": "' + b"sensitive-value" + b'"', "credential_literal"),
    ],
)
def test_reports_position_without_secret(value, rule):
    findings = security.inspect(b"first line\n" + value)
    assert (2, rule) in findings
    assert value.decode() not in str(findings)


def test_placeholders_and_examples_remain_publishable():
    assert not security.inspect(
        b'api_key="fixture-only-key"\n'
        b'"access_token": "YOUR_SEPARATE_SELARK_TOKEN"\n'
        b'"refresh_token": "fake-refresh-token"\n'
        b'"database": "~/.local/share/selark/records.sqlite3"'
    )


def test_git_guard_catches_forced_private_file_and_untracked_secret(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("server.json\n.env*\n!.env.example\n")
    (tmp_path / ".env.example").write_text("DOCUMENTED_PLACEHOLDER=\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".env.example"], check=True)
    (tmp_path / "server.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "server.json"], check=True)
    value = "sk-" + "Z" * 30
    (tmp_path / "report.txt").write_text(value)
    result = security.check(tmp_path)
    assert not result["passed"]
    assert {item["rule"] for item in result["findings"]} == {
        "tracked_private_or_ignored_file",
        "provider_key",
    }
    assert value not in str(result)
    assert all(set(item) == {"path", "line", "rule"} for item in result["findings"])
