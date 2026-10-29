"""Offline Linux acceptance checks for the two synthetic coding exercises.

Run in a restricted environment: python -I -S check_application_code.py answer.json a 3.
The answer file contains a `content` string. This is an acceptance harness, not a
general-purpose sandbox for arbitrary hostile Python. No model/network calls.
"""

import ast
import copy
import json
import random
import re
import resource
import sys

resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
resource.setrlimit(resource.RLIMIT_AS, (160 * 1024 * 1024, 160 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
with open(sys.argv[1]) as answer_file:
    raw = json.load(answer_file)
text = raw["content"]
variant = sys.argv[2]
turn = int(sys.argv[3])
blocks = re.findall(r"```python\s*\n(.*?)```", text, re.S)
result = {"tests": 0, "failures": 0, "examples": [], "status": "fail"}


def fail(message):
    result["failures"] += 1
    if len(result["examples"]) < 4:
        result["examples"].append(message[:250])


try:
    assert len(blocks) == 1, "Expected one Python fence"
    tree = ast.parse(blocks[0])
    allowed_attrs = {
        "get",
        "items",
        "values",
        "keys",
        "copy",
        "append",
        "extend",
        "strip",
        "index",
        "count",
    }
    for node in ast.walk(tree):
        assert not isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.ClassDef,
                ast.Global,
                ast.Nonlocal,
                ast.With,
                ast.AsyncFunctionDef,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
            ),
        ), "unsafe syntax"
        if isinstance(node, ast.Name):
            assert not node.id.startswith("__"), "dunder name"
        if isinstance(node, ast.Attribute):
            assert node.attr in allowed_attrs, "unapproved attribute"
        if isinstance(node, ast.Call):
            assert isinstance(node.func, (ast.Name, ast.Attribute)), "indirect callable"
            if isinstance(node.func, ast.Name):
                assert node.func.id not in {
                    "open",
                    "exec",
                    "eval",
                    "compile",
                    "getattr",
                    "setattr",
                    "delattr",
                    "globals",
                    "locals",
                    "vars",
                    "input",
                    "breakpoint",
                    "__import__",
                }, "unsafe call"
    assert all(
        isinstance(n, ast.FunctionDef)
        or isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for n in tree.body
    ), "top level side effects"
    builtins = {
        k: __builtins__.__dict__[k]
        for k in [
            "len",
            "list",
            "tuple",
            "dict",
            "set",
            "int",
            "str",
            "bool",
            "float",
            "isinstance",
            "type",
            "range",
            "enumerate",
            "zip",
            "min",
            "max",
            "sum",
            "any",
            "all",
            "sorted",
            "reversed",
            "ValueError",
            "TypeError",
            "KeyError",
        ]
    }
    space = {"__builtins__": builtins}
    exec(compile(tree, "generated-function", "exec"), space)
    fn = space["paginate" if variant == "a" else "dedupe_events"]

    def test(args, kwargs, expected=None, invalid=False):
        result["tests"] += 1
        original = copy.deepcopy(args)
        try:
            actual = fn(*args, **kwargs)
            if invalid:
                fail("Expected ValueError: " + repr((args, kwargs)))
                return
            if actual != expected:
                fail("Wrong result: " + repr((args, kwargs, expected, actual)))
            if args != original:
                fail("Mutated input")
            if variant == "a" and actual["items"] is args[0]:
                fail("Returned original list")
            if variant == "b" and any(a is b for a in actual for b in args[0]):
                fail("Returned original event dict")
        except ValueError:
            if not invalid:
                fail("Unexpected ValueError: " + repr((args, kwargs)))
        except Exception as e:
            fail(type(e).__name__ + ": " + repr((args, kwargs)))

    if variant == "a":
        for n in [0, 1, 2, 7, 13, 23]:
            for page in [1, 2, 3, 9, 30]:
                for size in [1, 2, 5, 10]:
                    for clamp in [False, True] if turn >= 2 else [False]:
                        items = list(range(n))
                        pages = [items[i : i + size] for i in range(0, n, size)]
                        effective = (
                            min(page, len(pages)) if clamp and pages else 1 if clamp else page
                        )
                        expected = {
                            "items": pages[effective - 1] if 1 <= effective <= len(pages) else [],
                            "page": effective,
                            "page_size": size,
                            "total": n,
                            "total_pages": len(pages),
                        }
                        if turn == 3:
                            expected |= {
                                "has_next": bool(n and effective < len(pages)),
                                "has_previous": bool(n and effective > 1),
                            }
                        kw = {"clamp": clamp} if turn >= 2 else {}
                        test((items, page, size), kw, expected)
        for bad in [None, True, False, 0, -1, 1.0, "1", [], {}]:
            test(([1, 2], bad, 1), {}, invalid=True)
            test(([1, 2], 1, bad), {}, invalid=True)
            test(([1, 2], 1, 1), {"max_page_size": bad}, invalid=True)
        for items in [None, "abc", {}, set(), 3]:
            test((items, 1, 1), {}, invalid=True)
        test(([1], 1, 3), {"max_page_size": 2}, invalid=True)
        if turn >= 2:
            for bad in [0, 1, "yes", None, []]:
                test(([1], 1, 1), {"clamp": bad}, invalid=True)
        test(
            ((1, 2, 3), 2, 2),
            {},
            {"items": [3], "page": 2, "page_size": 2, "total": 3, "total_pages": 2}
            | ({"has_next": False, "has_previous": True} if turn == 3 else {}),
        )
    else:
        rng = random.Random(8421)
        for scoped in [False, True] if turn >= 2 else [False]:
            for prefer in ["first", "last", "highest_revision"] if turn == 3 else ["first", "last"]:
                for n in [0, 1, 2, 3, 8, 20]:
                    for _run in range(12):
                        events = [
                            {
                                "id": rng.choice(["x", " y ", "z"]),
                                "caller_id": rng.choice(["A", "B"]),
                                "revision": rng.randrange(4),
                                "data": i,
                                "nested": [],
                            }
                            for i in range(n)
                        ]

                        def key(e, scoped=scoped):
                            return (e["caller_id"], e["id"]) if scoped else e["id"]

                        keys = list(dict.fromkeys(key(e) for e in events))
                        expected = []
                        for k in keys:
                            candidates = [(j, e) for j, e in enumerate(events) if key(e) == k]
                            selected = (
                                candidates[0][1]
                                if prefer == "first"
                                else candidates[-1][1]
                                if prefer == "last"
                                else max(candidates, key=lambda x: (x[1]["revision"], x[0]))[1]
                            )
                            expected.append(selected.copy())
                        kw = {"prefer": prefer} | ({"scoped": scoped} if turn >= 2 else {})
                        test((events,), kw, expected)
        for bad in [None, (), {}, "abc", 1]:
            test((bad,), {}, invalid=True)
        for bad in [None, {}, "x", {"id": ""}, {"id": "  "}, {"id": True}, {"id": 4}]:
            test(([bad],), {}, invalid=True)
        for prefer in [None, False, "bad", 0]:
            test(([],), {"prefer": prefer}, invalid=True)
        if turn >= 2:
            for bad in [0, 1, "yes", None]:
                test(([],), {"scoped": bad}, invalid=True)
            for event in [{"id": "x"}, {"id": "x", "caller_id": ""}, {"id": "x", "caller_id": 2}]:
                test(([event],), {"scoped": True}, invalid=True)
        if turn == 3:
            for revision in [True, None, -1, 1.0, "1"]:
                test(
                    ([{"id": "x", "revision": revision}],),
                    {"prefer": "highest_revision"},
                    invalid=True,
                )
                test(
                    ([{"id": "x", "revision": revision}],),
                    {"prefer": "first"},
                    [{"id": "x", "revision": revision}],
                )
    result["status"] = "pass" if result["tests"] and result["failures"] == 0 else "fail"
except Exception as e:
    result["execution_error"] = type(e).__name__ + ": " + str(e)[:200]
print(json.dumps(result, ensure_ascii=False))
