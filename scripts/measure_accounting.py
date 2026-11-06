"""Compare admission and Dashboard scaling with isolated synthetic ledgers only."""

import argparse
import json
import os
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from selark.config import initialize
from selark.dashboard_data import query_filters, snapshot
from selark.runtime import Runtime
from selark.storage import Store, utc_now


def timings(values):
    return {"median_ms": round(statistics.median(values), 3), "samples_ms": values}


def measure_admission(root, rows, samples):
    store = Store(root / "records.sqlite3")
    try:
        caller = store.create_token("measurement", policy={"daily_token_limit": 10**9})["id"]
        resource = {
            "caller": caller,
            "provider": "mimo",
            "estimate": 300,
            "output": 16,
            "price": None,
            "alias": None,
        }

        def admit(identity):
            start = time.perf_counter()
            store.attempt(
                identity,
                "fake-model",
                None,
                "mimo-default",
                resource=resource,
                purpose="chat",
                started_at=utc_now(),
            )
            elapsed = (time.perf_counter() - start) * 1000
            store.finish(
                identity,
                http_status=200,
                duration_ms=1,
                state={
                    "caller_id": caller,
                    "connection_id": "mimo-default",
                    "model": "fake-model",
                    "actual_model": "fake-model",
                    "upstream_attempts": 1,
                    "upstream_result": "success",
                    "delivery_status": "sent",
                    "usage_final": True,
                    "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
                },
            )
            return round(elapsed, 3)

        admit("seed")
        with store.transaction() as db:
            for table in ("requests", "allocations"):
                seed = dict(db.execute(f"SELECT * FROM {table} WHERE request_id='seed'").fetchone())
                columns = list(seed)
                values = []
                for i in range(1, rows):
                    row = seed | {"request_id": f"history-{i}"}
                    if table == "requests":
                        row["original_request_id"] = row["request_id"]
                    values.append(tuple(row[key] for key in columns))
                db.executemany(
                    f"INSERT INTO {table} ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    values,
                )
        cold = admit("cold")
        measured = [admit(f"sample-{i}") for i in range(samples)]
        # Includes receipts, not a sequence of read-only cached queries.
        quota = store.quota(caller)
        assert quota["charged_or_reserved_tokens"] == 14 * (rows + samples + 1)
        return {"history_rows": rows, "cold_ms": cold, **timings(measured)}
    finally:
        store.close()


def measure_dashboard(root, rows, samples):
    initialize(root, "synthetic-measurement-key", models=["fake-model"])
    now = datetime.now(UTC)
    old = (now - timedelta(days=90)).isoformat()
    with Runtime(root) as runtime, runtime.store.transaction() as db:
        db.executemany(
            "INSERT INTO requests(request_id,started_at,caller_id,connection_id,model,"
            "purpose,status,upstream_attempts,usage_final,prompt_tokens,completion_tokens,"
            "total_tokens) VALUES(?,?,'legacy','mimo-default','fake-model',"
            "'chat','success',1,1,12,2,14)",
            ((f"history-{i}", old) for i in range(rows)),
        )
    measured = []
    for _ in range(samples):
        start = time.perf_counter()
        result = snapshot(root, query_filters({"period": "24h"}, now=now), offline=True)
        measured.append(round((time.perf_counter() - start) * 1000, 3))
        assert result["ok"] and result["usage"]["summary"]["requests"] == 0
    return {"history_rows": rows, "selected_rows": 0, **timings(measured)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    for name in list(os.environ):
        if name.startswith("SELARK_"):
            del os.environ[name]
    with tempfile.TemporaryDirectory(prefix="selark-accounting-") as temporary:
        root = Path(temporary)
        result = {
            "real_provider_requests": 0,
            "admission": [measure_admission(root / str(n), n, 25) for n in (1000, 10000)],
            "dashboard": [
                measure_dashboard(root / f"dashboard-{n}", n, 3) for n in (20000, 100000)
            ],
        }
    document = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(document + "\n")
    print(document)


if __name__ == "__main__":
    main()
