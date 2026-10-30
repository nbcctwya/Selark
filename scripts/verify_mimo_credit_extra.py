"""Explicit extra coverage using an existing MiMo acceptance budget, never a new budget."""

import argparse
import asyncio
import contextlib
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
from verify_mimo_credit import MODEL, Acceptance, fingerprint_originals, reserve_micros

from selark.config import ClientConfig, _write_private, config_directory, load_config
from selark.runtime import Runtime
from selark.schemas import ChatRequest
from selark.service import service_lock_held
from selark.storage import Store


async def run(args):
    root = args.work_dir.resolve()
    report = json.loads((root / "report.json").read_text())
    budget = json.loads((root / "budget.json").read_text())
    assert report["temporary_service_stopped"]
    if not report["transport_passed"]:
        assert args.reviewed_stop_note, "Review the failure before explicitly starting new cases"
        review = json.loads(args.reviewed_stop_note.read_text())
        assert review["reviewed"] and review["budget_attempts"] == budget["attempts"]
        assert review["reserved_micros"] == budget["reserved_micros"]
        assert review["repeat_failed_cases"] is False
        assert review["successful_records"] == sum(
            row["status"] == "success" for row in report["records"]
        )
        # This continuation is only for a reviewed, entirely accounted-for prior run.
        assert review["successful_records"] == budget["attempts"]
        assert (root / "STOP").exists()
    else:
        assert not (root / "STOP").exists()
        review = None
    assert not service_lock_held(root / "profile/data")
    assert budget["maximum_micros"] <= 8000000
    assert not report.get("extended_acceptance"), "Do not repeat an executed extension"
    driver = Acceptance(
        SimpleNamespace(
            work_dir=root,
            python=args.python,
            verified_gift_cny=Decimal(report["gift_credit_reported_cny"]),
            max_reserve_cny=Decimal(budget["maximum_micros"]) / 1000000,
        )
    )
    _write_private(root / "base-report.json", report)
    original_passed = report["transport_passed"]
    if review:
        (root / "STOP").rename(root / "STOP-reviewed-original")
    driver.report = report
    driver.report.pop("finished_at", None)
    driver.report["temporary_service_stopped"] = False
    driver.report["extended_acceptance"] = {
        "started_at": datetime.now(UTC).isoformat(),
        "reviewed_prior_stop": review,
        "local_client_keepalive_expiry_seconds": 1,
    }
    driver.original = config_directory()
    driver.before = fingerprint_originals(driver.original)
    driver.url = load_config(driver.profile / "client.json", ClientConfig).url
    with Runtime(driver.profile) as runtime:
        for name in ("primary", "secondary"):
            caller = runtime.store.create_token(
                "extra-acceptance-" + name,
                policy={
                    "allowed_connections": ["mimo-default"],
                    "allowed_models": [f"mimo-default::{MODEL}"],
                },
            )
            driver.tokens[name] = caller["token"]
    driver.save()

    async def invoke(label, prompt, **options):
        payload = ChatRequest(
            messages=options.get("messages") or [{"role": "user", "content": prompt}],
            max_tokens=options.get("cap", 256),
        ).upstream_payload(MODEL)
        current = json.loads((root / "budget.json").read_text())
        if current["reserved_micros"] + reserve_micros(payload) > current["maximum_micros"]:
            driver.report["extended_acceptance"]["stopped_for_budget"] = True
            raise RuntimeError("Coverage budget exhausted; never reset it to continue")
        return await driver.call(label, prompt, **options)

    async with httpx.AsyncClient(
        trust_env=False, timeout=220, limits=httpx.Limits(keepalive_expiry=1)
    ) as driver.http:
        try:
            await driver.start()
            if args.matrix_calls:
                assert args.matrix_start is not None
                labels = {item["case"] for item in report["calls"]}
                assert not any(
                    f"matrix-{i:04}" in labels
                    for i in range(args.matrix_start, args.matrix_start + args.matrix_calls)
                ), "Do not repeat failed or completed generations"
                await driver.matrix(args.matrix_calls, start=args.matrix_start)
                if driver.failed:
                    raise RuntimeError("Continuation stopped; inspect the recorded outcome")
            # Close the stream/content-shape cross-product missing from the initial soak.
            for stream in (False, True):
                for temperature in (0.0, 0.8):
                    for case, prompt, expected in (
                        ("arithmetic", "Compute 179+263. Reply only with the integer.", "442"),
                        ("unicode", "Repeat exactly: 中文🌱 café Ελληνικά", "中文🌱 café Ελληνικά"),
                        (
                            "raw-json",
                            'Return raw JSON only, no fences: {"ok":true,"none":null}',
                            None,
                        ),
                    ):
                        await invoke(
                            f"extra-{case}-{stream}-{temperature}",
                            prompt,
                            stream=stream,
                            cap=128,
                            options={"temperature": temperature, "top_p": 0.7},
                            expectation=(
                                lambda text, _, expected=expected: text.strip() == expected
                            )
                            if expected
                            else lambda text, _: json.loads(text) == {"ok": True, "none": None},
                        )
            for stream in (False, True):
                for label, stop in (("string", "|END|"), ("list", ["|END|"]), ("unicode", "结束")):
                    sequence = stop[0] if isinstance(stop, list) else stop
                    await invoke(
                        f"extra-stop-{label}-{stream}",
                        f"Return exactly: BEFORE{sequence}AFTER",
                        stream=stream,
                        cap=32,
                        options={"stop": stop, "temperature": 0.0},
                        expectation=lambda text, _: text == "BEFORE",
                    )
            # Larger inputs remain below the immutable transport's 500k-byte guard.
            for size in (100000, 200000, 400000):
                for stream in (False, True):
                    marker = f"CHECK-{size}-{int(stream)}"
                    filler = "Archive record: green shelf; 17 books; status verified.\n" * (
                        size // 56
                    )
                    middle = len(filler) // 2
                    prompt = (
                        "Find SPECIAL_MARKER and return its exact value only.\n"
                        + filler[:middle]
                        + "\nSPECIAL_MARKER="
                        + marker
                        + "\n"
                        + filler[middle:]
                    )
                    await invoke(
                        f"extra-context-{size}-{stream}",
                        prompt,
                        cap=96,
                        stream=stream,
                        expectation=lambda text, _, marker=marker: text.strip() == marker,
                    )
            # Give a reproducible failing boundary case back to the model once, explicitly.
            previous = next(c for c in report["calls"] if c["case"] == "task-coding-a-3")
            prompt = (
                "Correct this implementation using the concrete failing test. "
                "paginate([], 2, 1, clamp=True) must return page=1, total_pages=0, items=[], "
                "has_next=False, has_previous=False. Preserve exact-int validation (reject bool), "
                "max_page_size=50, new result lists, out-of-range behavior when clamp=False, "
                "and every other contract. Return one complete Python function in one fence; "
                "no imports, I/O, or extra examples.\n" + previous["content"]
            )
            await invoke("extra-coding-explicit-counterexample", prompt, stream=True, cap=4096)
            for cap in (512, 2048, 8192):
                await invoke(
                    f"extra-long-output-{cap}",
                    "Count every integer from 1 through 10000, separated by single spaces. "
                    "Do not summarize or skip numbers; continue until the output limit.",
                    cap=cap,
                    stream=True,
                    expectation=lambda _, finish: finish == "length",
                )
            if driver.failed:
                raise RuntimeError("Real outcome needs review; no further generation")

            # Installed CLI, independent of an activated project environment.
            for name, extra, stdin in (
                ("ordinary", ["Reply exactly CLI_OK", "--json"], None),
                ("stream", ["Reply exactly CLI_STREAM_OK", "--stream"], None),
                ("stdin", ["-", "--json"], "Reply exactly CLI_STDIN_OK"),
                ("alias", ["Reply exactly CLI_ALIAS_OK", "--alias", "mimo-live", "--json"], None),
            ):
                before = {r["request_id"] for r in driver.records()}
                result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        args.python,
                        "-m",
                        "selark",
                        "--config-dir",
                        str(driver.profile),
                        "ask",
                        *extra,
                        "--max-tokens",
                        "32",
                    ],
                    input=stdin,
                    text=True,
                    capture_output=True,
                    timeout=200,
                    cwd=root,
                )
                for _ in range(200):
                    rows = [r for r in driver.records() if r["request_id"] not in before]
                    if rows and all(row["status"] != "running" for row in rows):
                        break
                    await asyncio.sleep(0.05)
                passed = (
                    result.returncode == 0
                    and len(rows) == 1
                    and rows[0]["usage_known"]
                    and rows[0]["upstream_attempts"] == 1
                    and rows[0]["status"] == "success"
                )
                driver.report["calls"].append(
                    {
                        "case": "extra-cli-" + name,
                        "transport_passed": passed,
                        "record": rows[0] if len(rows) == 1 else None,
                        "exit_code": result.returncode,
                        "stream": name == "stream",
                    }
                )
                driver.check("installed_cli_" + name, passed)

            before = json.loads((root / "budget.json").read_text())["attempts"]
            too_large = await driver.http.post(
                driver.url + "/v1/chat/completions",
                headers={"Authorization": "Bearer " + driver.tokens["primary"]},
                json={"messages": [{"role": "user", "content": "x" * (1024 * 1024)}]},
            )
            driver.check(
                "oversized_body_rejected_without_generation",
                too_large.status_code == 413
                and json.loads((root / "budget.json").read_text())["attempts"] == before,
            )
            await driver.operations()
            await driver.call(
                "extra-client-disconnect",
                "Count from 1 to 100, separated by spaces.",
                stream=True,
                cap=128,
                disconnect=True,
            )
            if driver.failed:
                raise RuntimeError("Disconnect outcome needs review")
            # One final intentional deadline. Any unknown bill remains fully reserved.
            await driver.stop()
            document = json.loads((driver.profile / "server.json").read_text())
            original_timeout = document["limits"]["request_timeout_seconds"]
            document["limits"]["request_timeout_seconds"] = 1.0
            _write_private(driver.profile / "server.json", document)
            try:
                await driver.start()
                attempts = json.loads((root / "budget.json").read_text())["attempts"]
                response = await driver.http.post(
                    driver.url + "/v1/chat/completions",
                    headers={"Authorization": "Bearer " + driver.tokens["primary"]},
                    json={
                        "messages": [{"role": "user", "content": "Count from 1 to 10000."}],
                        "max_tokens": 512,
                    },
                )
                identity = response.headers.get("x-request-id")
                for _ in range(200):
                    row = next((r for r in driver.records() if r["request_id"] == identity), None)
                    if row and row["status"] != "running":
                        break
                    await asyncio.sleep(0.05)
                error = response.json().get("error", {}).get("code")
                passed = bool(
                    response.status_code == 504
                    and error == "request_deadline_exceeded"
                    and row
                    and row["error_code"] == error
                    and row["status"] == "failed"
                    and json.loads((root / "budget.json").read_text())["attempts"] == attempts + 1
                )
                driver.report["calls"].append(
                    {
                        "case": "extra-intentional-deadline",
                        "transport_passed": passed,
                        "expected_timeout": True,
                        "http_status": response.status_code,
                        "error_code": error,
                        "request_id": identity,
                        "record": row,
                        "max_output_tokens": 512,
                        "stream": False,
                    }
                )
                driver.check("intentional_deadline_has_one_attempt_and_explicit_record", passed)
                health = (await driver.http.get(driver.url + "/health")).json()
                driver.check(
                    "deadline_releases_capacity",
                    health["capacity"]["active"] == 0 and health["capacity"]["queued"] == 0,
                )
            finally:
                await driver.stop()
                document["limits"]["request_timeout_seconds"] = original_timeout
                _write_private(driver.profile / "server.json", document)
        except Exception as exc:
            driver.failed = True
            driver.report["extended_acceptance"]["failure_type"] = type(exc).__name__
        finally:
            await driver.stop()
            with contextlib.closing(
                Store(driver.profile / "data/records.sqlite3", readonly=True)
            ) as store:
                driver.report["summary"] = store.summary({})["summary"]
                driver.report["records"] = store.records({}, limit=10000)
            driver.report["original_files_unchanged"] &= (
                fingerprint_originals(driver.original) == driver.before
            )
            driver.report["finished_at"] = datetime.now(UTC).isoformat()
            driver.report["transport_passed"] = original_passed and not driver.failed
            driver.report["temporary_service_stopped"] = True
            driver.report["extended_acceptance"]["passed"] = not driver.failed
            driver.save()
            print(
                json.dumps(
                    {
                        "extra_complete": not driver.failed,
                        "calls": len(driver.report["calls"]),
                        "summary": driver.report["summary"],
                    }
                ),
                flush=True,
            )
    return int(driver.failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--reviewed-stop-note", type=Path)
    parser.add_argument("--matrix-start", type=int)
    parser.add_argument("--matrix-calls", type=int, default=0)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
