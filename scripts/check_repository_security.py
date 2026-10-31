"""Offline publication guard. Report file/line/rule only, never credential contents.

This catches common credential formats and accidentally tracked private files;
it complements manual review, not a guarantee that every secret can be detected.
No user configuration, environment credentials or network services are accessed.
"""

import json
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "provider_key": rb"\bsk-(?:proj-|ant-api\d+-|svcacct-)?[A-Za-z0-9_-]{20,}",
    "github_token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "aws_access_key": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "jwt": rb"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}",
    "bearer_literal": rb"(?i)\bBearer\s+[A-Za-z0-9_./+=-]{24,}",
    "personal_home_path": rb"(?:/home/|/Users/)[A-Za-z0-9_.-]+",
}
ASSIGNMENT = re.compile(
    rb"""(?i)["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)["']?\s*[:=]\s*["']([^"'\r\n]{12,})["']"""
)
PLACEHOLDERS = (b"fake-", b"test-", b"fixture-", b"YOUR_", b"<REDACTED", b"[REDACTED")


def inspect(data):
    findings = set()
    for name, pattern in PATTERNS.items():
        for match in re.finditer(pattern, data):
            findings.add((data[: match.start()].count(b"\n") + 1, name))
    for match in ASSIGNMENT.finditer(data):
        if not match.group(1).startswith(PLACEHOLDERS):
            findings.add((data[: match.start()].count(b"\n") + 1, "credential_literal"))
    return sorted(findings)


def git(root, *args, input=None):
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, capture_output=True, check=False
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Git could not enumerate files for the publication check.")
    return result.stdout


def check(root):
    tracked = git(root, "ls-files", "-z")
    ignored = git(root, "check-ignore", "--no-index", "-v", "-z", "--stdin", input=tracked)
    fields = ignored.split(b"\0")
    findings = [
        {"path": name.decode(), "line": 0, "rule": "tracked_private_or_ignored_file"}
        for pattern, name in zip(fields[2::4], fields[3::4], strict=True)
        if not pattern.startswith(b"!")
    ]
    names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    checked = 0
    for name in sorted(set(names.split(b"\0")) - {b""}):
        path = root / name.decode()
        if path.is_symlink():
            findings.append({"path": name.decode(), "line": 0, "rule": "review_symlink_target"})
            continue
        if not path.is_file():
            continue
        checked += 1
        findings.extend(
            {"path": name.decode(), "line": line, "rule": rule}
            for line, rule in inspect(path.read_bytes())
        )
    return {"checked_files": checked, "findings": findings, "passed": not findings}


def main():
    root = Path(__file__).resolve().parents[1]
    result = check(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
