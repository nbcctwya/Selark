"""The offline checker accepts ordinary container code without executing examples."""

import json
import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).resolve().parents[1] / "scripts/check_application_code.py"
IMPLEMENTATION = """
def dedupe_events(events, *, prefer='first'):
    if not isinstance(events, list) or prefer not in ('first', 'last'):
        raise ValueError()
    seen, order, selected = set(), [], {}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError()
        identity = event.get('id')
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError()
        if identity not in seen:
            seen.add(identity)
            order.append(identity)
        if prefer == 'last' or identity not in selected:
            selected[identity] = dict(event)
    return [selected[identity] for identity in order]
"""


def check(tmp_path, content):
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"content": content}))
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(CHECKER), str(answer), "b", "1"],
        capture_output=True,
        text=True,
        timeout=8,
        cwd=tmp_path,
    )
    return json.loads(result.stdout)


def test_set_add_and_separate_examples_are_accepted_without_executing_examples(tmp_path):
    result = check(
        tmp_path,
        "```python\n" + IMPLEMENTATION + "\n```\n"
        "Example, excluded from execution:\n```python\n"
        "open('example-must-not-run', 'w').write('unsafe')\n```",
    )
    assert result["status"] == "pass" and result["tests"] > 100
    assert not (tmp_path / "example-must-not-run").exists()


def test_implementation_side_effects_are_still_rejected(tmp_path):
    result = check(
        tmp_path,
        "```python\n" + IMPLEMENTATION + "\nopen('must-not-run', 'w').write('unsafe')\n```",
    )
    assert result["status"] == "fail" and result["tests"] == 0
    assert not (tmp_path / "must-not-run").exists()
