"""Run a bounded synthetic reference qualification, or independently grade its evidence.

This development tool does not certify a release, invoke real providers, install
packages, change system tuning, or operate a user's service. Collection currently
targets Linux / WSL; offline grading is platform independent.
"""

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
# These are acceptance policy, not values supplied by the evidence being graded.
POLICY = {"rounds": 3, "pairs": 300, "idle_seconds": 30, "p95_ms": 10, "rss_mib": 72, "cpu": 0.5}
MODES = ("nonstream", "stream_first_text", "stream_complete")
RECOVERY_CHECKS = {
    "cli_and_passive_checks",
    "ordinary_and_sse_over_real_local_sockets",
    "bounded_queue_overload_and_wait_timeout",
    "actual_socket_read_idle_timeout",
    "trickling_stream_hits_total_deadline",
    "ordinary_client_socket_disconnect",
    "crash_restart_preserves_unknown_usage_without_retry",
    "bounded_shutdown_grace_and_accounting",
    "post_recovery_call_and_usage_totals",
}
STEPS = [
    (
        "invariants",
        "scripts.measure_runtime",
        45,
        ["--check-invariants", "--idle-seconds", "2", "--requests", "10"],
    ),
    ("recovery", "scripts.verify_stability", 90, []),
    *[
        (f"relay-{i}", "scripts.measure_relay", 120, ["--samples", "300", "--idle-seconds", "30"])
        for i in range(1, POLICY["rounds"] + 1)
    ],
]


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("missing, negative or non-finite measurement")
    return value


def assess(evidence):
    """Recompute signed paired percentiles; never trust saved pass flags or a median."""
    checks, rounds = [], []

    def check(group, name, passed):
        checks.append({"group": group, "check": name, "passed": bool(passed)})

    try:
        check(
            "evidence",
            "schema_and_complete",
            evidence["schema"] == 1 and evidence["complete"] is True,
        )
        initial = evidence["fingerprint"]
        check("evidence", "frozen_inputs", initial == evidence["final_fingerprint"])
        check(
            "evidence",
            "measured_install_matches_source",
            initial["runtime"] == initial["installed_runtime"],
        )
        check("evidence", "reference_platform", initial["platform"] == "Linux")
        for key in ("runtime", "installed_runtime", "harness", "dependencies_lock"):
            value = initial[key]
            check(
                "evidence",
                f"fingerprint_{key}",
                isinstance(value, str)
                and len(value) == 64
                and all(c in "0123456789abcdef" for c in value),
            )
        steps = evidence["steps"]
        names = [item["name"] for item in steps]
        check("evidence", "all_stages_once_in_order", names == [step[0] for step in STEPS])
        for item in steps:
            name = item["name"]
            check(
                "evidence", f"{name}_completed", item["exit_code"] == 0 and "error_type" not in item
            )
            check("evidence", f"{name}_same_inputs", item["before"] == initial == item["after"])
        by_name = {item["name"]: item["report"] for item in steps}
        invariant = by_name["invariants"]
        check(
            "invariants",
            "zero_extra_generations_and_exact_usage",
            invariant["requests"] == 10
            and invariant["fake_generation_requests"] == 11
            and invariant["reported_tokens"] == 154,
        )
        check("invariants", "no_real_provider", invariant["real_provider_requests"] == 0)
        check(
            "invariants", "caller_text_preserved", invariant["forwarded_messages_unchanged"] is True
        )
        check(
            "invariants",
            "idle_scans_and_optional_imports",
            finite(invariant["idle_seconds"]) >= 2
            and invariant["idle_journal_scans"] == 0
            and invariant["jwt_loaded"] is False,
        )
        recovery = by_name["recovery"]
        check(
            "recovery",
            "synthetic_only",
            recovery["real_mimo_requests"] == 0 and recovery["simulated_upstream_requests"] == 9,
        )
        outcomes = recovery["checks"]
        check(
            "recovery",
            "required_socket_and_recovery_checks",
            len(outcomes) == len(RECOVERY_CHECKS)
            and {item["check"] for item in outcomes} == RECOVERY_CHECKS
            and all(item["passed"] is True for item in outcomes),
        )
        check(
            "recovery",
            "known_usage_after_restart",
            next(
                item for item in outcomes if item["check"] == "post_recovery_call_and_usage_totals"
            )["known_total_tokens"]
            == 56,
        )
        check(
            "recovery",
            "passive_operations_do_not_generate",
            next(item for item in outcomes if item["check"] == "cli_and_passive_checks")[
                "upstream_calls"
            ]
            == 0,
        )
        for index in range(1, POLICY["rounds"] + 1):
            name = f"relay-{index}"
            report = by_name[name]
            check(
                "evidence",
                f"{name}_reference_method",
                not report.get("failed")
                and "profile" not in report
                and report["samples_per_mode"] == POLICY["pairs"]
                and report["warmup_pairs_per_mode"] == 5
                and report["concurrency"] == 1
                and report["chunked_sse"] is False
                and report["loopback_proxy_bypassed"] is True,
            )
            check(
                "evidence",
                f"{name}_environment",
                all(report[k] == initial[k] for k in ("python", "platform", "machine")),
            )
            check(
                "invariants",
                f"{name}_dispatch_and_accounting",
                report["real_provider_requests"] == 0
                and report["extra_generations"] == 0
                and report["fake_direct_generations"] == 610
                and report["fake_relay_generations"] == 610
                and report["reported_relay_tokens"] == 8540
                and report["forwarded_payloads_unchanged"] is True,
            )
            metrics = {"round": index}
            for mode in MODES:
                data = report[mode]
                pairs = data["pairs"]
                check("evidence", f"{name}_{mode}_sample_count", len(pairs) == POLICY["pairs"])
                differences = sorted(
                    finite(pair["relay_ms"]) - finite(pair["direct_ms"]) for pair in pairs
                )
                if not differences:
                    raise ValueError("empty sample")
                percentiles = {
                    label: round(differences[math.ceil(q * len(differences)) - 1], 4)
                    for label, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
                }
                check(
                    "evidence",
                    f"{name}_{mode}_summary_matches_samples",
                    percentiles == data["overhead_ms"]
                    and data["negative_differences"] == sum(value < 0 for value in differences),
                )
                metrics[mode] = percentiles
                if mode != "stream_complete":
                    check(
                        "performance", f"{name}_{mode}_p95", percentiles["p95"] <= POLICY["p95_ms"]
                    )
            idle = report["idle"]
            seconds = finite(idle["seconds"])
            check("evidence", f"{name}_idle_window", seconds >= POLICY["idle_seconds"])
            cpu = 100 * finite(idle["cpu_seconds"]) / seconds
            rss = finite(idle["rss_bytes"]) / 1048576
            check("evidence", f"{name}_rss_measurable", rss > 0)
            check("invariants", f"{name}_idle_generations", idle["upstream_requests"] == 0)
            check("performance", f"{name}_idle_cpu", cpu <= POLICY["cpu"])
            check("performance", f"{name}_idle_rss", rss <= POLICY["rss_mib"])
            metrics.update(idle_cpu_percent=cpu, idle_rss_mib=rss)
            rounds.append(metrics)
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        ZeroDivisionError,
        StopIteration,
        OverflowError,
    ):
        check("evidence", "complete_well_formed_measurements", False)
    groups = {
        group: any(item["group"] == group for item in checks)
        and all(item["passed"] for item in checks if item["group"] == group)
        for group in ("evidence", "invariants", "recovery", "performance")
    }
    groups["performance"] = groups["performance"] and len(rounds) == POLICY["rounds"]
    return {
        "scope": "single-connection relay reference; not release certification",
        "policy": POLICY,
        "qualified": all(groups.values()),
        "groups": groups,
        "failed_checks": [item for item in checks if not item["passed"]],
        "checks": checks,
        "rounds": rounds,
    }


def tree_hash(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.py")):
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def fingerprint(env):
    # Match the core's import environment outside the checkout, without PYTHONPATH.
    probe = """
import hashlib, json, platform
from importlib.metadata import version
from pathlib import Path
import selark
root = Path(selark.__file__).resolve().parent
digest = hashlib.sha256()
for path in sorted(root.rglob('*.py')):
    digest.update(path.relative_to(root).as_posix().encode() + b'\\0' + path.read_bytes())
packages = ('anyio', 'fastapi', 'httpx', 'pydantic', 'uvicorn')
print(json.dumps({'installed_runtime': digest.hexdigest(),
    'python': platform.python_version(), 'platform': platform.system(),
    'machine': platform.machine(), 'packages': {name: version(name) for name in packages}}))
"""
    with tempfile.TemporaryDirectory(prefix="selark-qualification-import-") as outside:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=outside,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    return json.loads(result.stdout) | {
        "runtime": tree_hash(REPOSITORY / "src/selark"),
        "harness": tree_hash(REPOSITORY / "scripts"),
        "dependencies_lock": hashlib.sha256(
            (REPOSITORY / "requirements.lock").read_bytes()
        ).hexdigest(),
    }


def save(path, document):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def run_step(command, log, env, timeout):
    with (
        log.open("w", encoding="utf-8") as output,
        subprocess.Popen(
            command,
            cwd=REPOSITORY,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as process,
    ):
        try:
            return process.wait(timeout=timeout)
        finally:
            # Only the new process group owned by this invocation is signalled.
            # Give fixture finally-blocks a chance to close their temporary core.
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGINT)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            # A child can outlive a parent that exited, failed or handled SIGINT.
            # A group with no remaining processes simply raises ProcessLookupError.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def collect(directory):
    directory.mkdir(parents=True, exist_ok=False)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SELARK_")
        and key not in {"PYTHONPATH", "PYTHONOPTIMIZE", "MIMO_API_KEY"}
    }
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    started = time.monotonic()
    evidence = {
        "schema": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "complete": False,
        "steps": [],
    }
    try:
        evidence["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True, timeout=10
        ).strip()
        evidence["worktree_dirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=REPOSITORY, text=True, timeout=10
            ).strip()
        )
        initial = evidence["fingerprint"] = fingerprint(env)
        if initial["runtime"] != initial["installed_runtime"]:
            raise ValueError("installed runtime differs from checkout")
        for name, module, timeout, arguments in STEPS:
            item = {
                "name": name,
                "timeout_seconds": timeout,
                "before": fingerprint(env),
                "exit_code": None,
            }
            step_started = time.monotonic()
            evidence["steps"].append(item)
            save(directory / "evidence.json", evidence)
            print(json.dumps({"phase": name, "status": "running"}), flush=True)
            try:
                if item["before"] != initial:
                    raise ValueError("inputs changed during qualification")
                path = directory / f"{name}.json"
                item["exit_code"] = run_step(
                    [sys.executable, "-m", module, *arguments, "--output", str(path)],
                    directory / f"{name}.log",
                    env,
                    timeout,
                )
                if path.exists():
                    item["report"] = json.loads(path.read_text(encoding="utf-8"))
                item["after"] = fingerprint(env)
                if item["exit_code"] != 0 or item["after"] != initial or "report" not in item:
                    raise ValueError("stage failed or its evidence is incomplete")
            except (Exception, KeyboardInterrupt) as error:
                item["error_type"] = type(error).__name__
                raise
            finally:
                item["elapsed_seconds"] = round(time.monotonic() - step_started, 3)
                save(directory / "evidence.json", evidence)
        evidence["complete"] = True
    except (Exception, KeyboardInterrupt) as error:
        evidence["error_type"] = type(error).__name__
    finally:
        try:
            evidence["final_fingerprint"] = fingerprint(env)
        except Exception as error:
            evidence["final_fingerprint"] = {"error_type": type(error).__name__}
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save(directory / "evidence.json", evidence)
    result = assess(evidence)
    save(directory / "assessment.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--output",
        type=Path,
        help="Create a NEW evidence directory and run qualification (Linux / WSL).",
    )
    mode.add_argument(
        "--check-evidence",
        type=Path,
        help="Read-only, offline recomputation from an existing evidence.json.",
    )
    args = parser.parse_args()
    if args.output:
        if platform.system() != "Linux" or sys.flags.optimize:
            parser.error("collection requires Linux / WSL and Python assertions enabled")
        directory = args.output.resolve()
        if directory.exists():
            parser.error("output directory must be new; previous evidence is never overwritten")
        result = collect(directory)
    else:
        try:
            evidence = json.loads(args.check_evidence.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            evidence = {}
        result = assess(evidence)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "checks"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["qualified"] else (1 if result["groups"]["evidence"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
