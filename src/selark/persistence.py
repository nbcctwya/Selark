"""Bounded asynchronous storage, durable outcome receipts, and idempotent replay."""

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from selark.config import ConfigError, _write_private, private_directory
from selark.locking import file_lock
from selark.storage import utc_now

logger = logging.getLogger("selark.storage")
FIELDS = frozenset(
    [
        "request_id",
        "started_at",
        "finished_at",
        "caller_id",
        "connection_id",
        "purpose",
        "model",
        "actual_model",
        "session_id",
        "fingerprint",
        "upstream_attempts",
        "usage",
        "usage_final",
        "upstream_status",
        "stream_status",
        "error_code",
        "error_message",
        "upstream_result",
        "delivery_status",
        "upstream_error_code",
        "delivery_error_code",
        "queue_ms",
        "record_status",
        "record_revision",
        "cancelled",
        "upstream_completed_at",
    ]
)


def snapshot(state, http_status, duration_ms, *, settled):
    return {
        "schema_version": 1,
        "state": {k: v for k, v in state.items() if k in FIELDS}
        | {"record_revision": 2 if settled else 1},
        "http_status": http_status,
        "duration_ms": duration_ms,
        "settled": settled,
    }


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Journal:
    def __init__(self, directory: Path):
        self.directory = directory
        private_directory(directory)

    def save(self, document):
        identity = document["state"]["request_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
            raise ValueError("invalid receipt identity")
        with file_lock(self.directory.parent / "recovery.lock"):
            _write_private(self.directory / (identity + ".json"), document)
            sync_directory(self.directory)

    def remove(self, identity):
        with file_lock(self.directory.parent / "recovery.lock"):
            self._remove(identity)

    def _remove(self, identity):
        (self.directory / (identity + ".json")).unlink(missing_ok=True)
        sync_directory(self.directory)

    def entries(self):
        return sorted(self.directory.glob("*.json"))

    def check_writable(self):
        path = self.directory / ".write-check"
        _write_private(path, {"check": True})
        path.unlink()
        sync_directory(self.directory)

    @staticmethod
    def load(path):
        info = path.stat()
        if (
            path.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise ValueError("unsafe receipt")
        value = json.loads(path.read_bytes())
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(value.get("state"), dict)
            or value["state"].get("request_id") != path.stem
            or not value["state"].get("started_at")
            or type(value.get("http_status")) is not int
            or not 100 <= value["http_status"] <= 599
            or type(value.get("settled")) is not bool
            or type(value.get("duration_ms")) not in {int, float}
            or not 0 <= value["duration_ms"] <= 2**63 - 1
        ):
            raise ValueError("invalid receipt")
        state = value["state"]
        if set(state) - FIELDS or state.get("record_revision") != (2 if value["settled"] else 1):
            raise ValueError("invalid receipt state")
        for key, item in state.items():
            if item is None:
                continue
            if key == "usage":
                valid = isinstance(item, dict)
            elif key in {"usage_final", "cancelled"}:
                valid = type(item) is bool
            elif key in {"upstream_attempts", "upstream_status", "record_revision"}:
                valid = type(item) is int and 0 <= item <= 2**63 - 1
            elif key == "queue_ms":
                valid = type(item) in {int, float} and 0 <= item <= 2**63 - 1
            else:
                valid = isinstance(item, str)
            if not valid:
                raise ValueError("invalid receipt field")
        for key in ("started_at", "finished_at", "upstream_completed_at"):
            if state.get(key) and datetime.fromisoformat(state[key]).tzinfo is None:
                raise ValueError("receipt time must include timezone")
        if (
            state.get("purpose") == "connection_probe"
            and state.get("upstream_attempts")
            and (not state.get("connection_id") or not state.get("fingerprint"))
        ):
            raise ValueError("missing probe identity")
        return value

    def replay(self, store, *, include_unsettled=False, limit=None):
        with file_lock(self.directory.parent / "recovery.lock"):
            return self._replay(store, include_unsettled=include_unsettled, limit=limit)

    def _replay(self, store, *, include_unsettled=False, limit=None):
        restored, invalid, failed = 0, 0, 0
        paths = self.entries()
        for path in paths:
            if limit and restored + failed >= limit:
                break
            try:
                document = self.load(path)
            except (OSError, ValueError, TypeError, AttributeError):
                invalid += 1
                continue
            if not document["settled"]:
                if not include_unsettled:
                    continue
                document["state"].update(
                    record_status="interrupted",
                    delivery_status="unknown",
                    error_code="service_restarted",
                    error_message="交付确认前退出；保留已取得的上游结果和用量。",
                    finished_at=utc_now(),
                )
            try:
                apply_document(store, document)
                self._remove(path.stem)
                restored += 1
            except (sqlite3.Error, OSError):
                failed += 1
                break  # No busy retry loop or model regeneration.
        return {
            "restored": restored,
            "invalid": invalid,
            "failed": failed,
            "pending": len(self.entries()),
        }


def apply_document(store, document):
    store.finish(
        document["state"]["request_id"],
        state=document["state"],
        http_status=document["http_status"],
        duration_ms=document["duration_ms"],
    )


class AsyncLedger:
    def __init__(self, store, limits, journal=None):
        self.store, self.limits, self.journal = store, limits, journal
        self.db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selark-db")
        self.io_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selark-receipts")
        self.pending = 0
        self.degraded = False
        self.invalid = 0
        self.closed = False
        self.recovery_task = None
        self.receipt_lock = asyncio.Lock()

    async def call(self, method, *args, **kwargs):
        work = functools.partial(getattr(self.store, method), *args, **kwargs)
        return await self.wait_work(self.db_pool, work)

    @staticmethod
    async def wait_work(pool, work):
        future = asyncio.get_running_loop().run_in_executor(pool, work)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A cancelled await cannot stop sqlite3/fsync. Retain serialization until
            # that bounded operation finishes, so replay cannot delete a newer receipt.
            with contextlib.suppress(Exception):
                await asyncio.shield(future)
            raise

    async def io(self, function, *args, **kwargs):
        work = functools.partial(function, *args, **kwargs)
        return await self.wait_work(self.io_pool, work)

    async def start(self):
        await self.call("set_busy_timeout", self.limits.storage_busy_timeout_seconds)
        if self.journal:
            self.pending = len(await self.io(self.journal.entries))
            self.recovery_task = asyncio.create_task(self.recovery_loop())

    def available(self):
        return (
            not self.closed and not self.degraded and self.pending < self.limits.max_pending_records
        )

    def health(self):
        return {
            "degraded": self.degraded,
            "pending_records": self.pending,
            "invalid_records": self.invalid,
            "accepting": self.available(),
        }

    async def persist(self, state, http_status, duration_ms, *, settled):
        document = snapshot(state, http_status, duration_ms, settled=settled)
        journal_ok = False
        async with self.receipt_lock:
            if self.journal:
                try:
                    await self.io(self.journal.save, document)
                    journal_ok = True
                except (OSError, ValueError):
                    self.degraded = True
            try:
                await self.call(
                    "finish",
                    state["request_id"],
                    state=document["state"],
                    http_status=http_status,
                    duration_ms=duration_ms,
                )
                result = "committed"
                if settled and journal_ok:
                    await self.io(self.journal.remove, state["request_id"])
            except (sqlite3.Error, OSError, ConfigError):
                result = "pending" if journal_ok else "failed"
                if not journal_ok:
                    self.degraded = True
                logger.error(
                    json.dumps(
                        {
                            "event": "record_write_failed",
                            "request_id": state["request_id"],
                            "persistence": result,
                            "usage": state.get("usage"),
                            "usage_final": state.get("usage_final", False),
                        }
                    )
                )
            if self.journal:
                try:
                    self.pending = len(await self.io(self.journal.entries))
                except OSError:
                    self.degraded = True
            return result

    async def replay(self):
        if not self.journal:
            return
        async with self.receipt_lock:
            # Serialize replay with receipt updates, but never block the event loop.
            report = await self.call_journal_replay()
            self.pending, self.invalid = report["pending"], report["invalid"]
            if report["restored"]:
                logger.info(json.dumps({"event": "records_recovered", **report}))
            if self.degraded and not self.invalid:
                await self.io(self.journal.check_writable)
            self.degraded = bool(self.invalid)

    async def call_journal_replay(self):
        work = functools.partial(self.journal.replay, self.store, limit=8)
        return await self.wait_work(self.db_pool, work)

    async def recovery_loop(self):
        while not self.closed:
            await asyncio.sleep(1)
            try:
                await self.replay()
            except (sqlite3.Error, OSError, ValueError):
                self.degraded = True

    async def close(self):
        self.closed = True
        if self.recovery_task:
            self.recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.recovery_task
        try:
            await self.replay()
        finally:
            await asyncio.to_thread(self.db_pool.shutdown, wait=True)
            await asyncio.to_thread(self.io_pool.shutdown, wait=True)
