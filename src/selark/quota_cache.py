"""Bounded, rebuildable admission totals; SQLite remains the authoritative ledger.

Only reuse totals inside a write transaction. Other connections' commits and
untracked writes on this connection invalidate them before admission. Deltas are
published after commit; rollback never changes a cached total.
"""

import json
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

from selark.errors import GatewayError
from selark.governance import allocation_rows, checked_allocation


@dataclass
class AllocationTotals:
    tokens: int = 0
    unknown_tokens: int = 0
    unknown_costs: int = 0
    currencies: dict = field(default_factory=dict)
    cost: Decimal = Decimal(0)

    def add(self, row, sign=1):
        self.tokens += sign * row["quota_tokens"]
        self.unknown_tokens += sign * (row["token_basis"] == "unknown_hold")
        self.unknown_costs += sign * (row["quota_cost"] is None or row["cost_status"] == "unknown")
        currency = row["currency"]
        count = self.currencies.get(currency, 0) + sign
        if count:
            self.currencies[currency] = count
        else:
            self.currencies.pop(currency, None)
        if row["quota_cost"] is not None:
            with localcontext() as context:
                context.prec = 60
                self.cost += sign * Decimal(row["quota_cost"])

    @classmethod
    def read(cls, db, caller, day):
        result = cls()
        for row in allocation_rows(db, caller, day):
            result.add(row)
        return result


class QuotaCache:
    MAX_ENTRIES = 64

    def __init__(self):
        self.entries = OrderedDict()
        self.revision = None

    def get(self, db, caller, day):
        key = (caller, day)
        if key not in self.entries:
            value = AllocationTotals.read(db, caller, day)
            if len(self.entries) >= self.MAX_ENTRIES:
                self.entries.popitem(last=False)
            self.entries[key] = value
        self.entries.move_to_end(key)
        return self.entries[key]

    @staticmethod
    def row(db, identity):
        row = db.execute(
            "SELECT a.*,r.usage_json AS request_usage_json FROM allocations a "
            "LEFT JOIN requests r ON r.request_id=a.request_id WHERE a.request_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            return None
        usage = json.loads(row["request_usage_json"]) if row["request_usage_json"] else None
        return checked_allocation(row, usage)

    @contextmanager
    def transaction(self, db, identity):
        version = None
        try:
            with db:
                # Establish a stable view before reading the version or cached totals.
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA data_version").fetchone()[0]
                if self.revision != (version, db.total_changes):
                    self.entries.clear()
                before = self.row(db, identity) if self.entries else None
                yield db
                after = self.row(db, identity) if self.entries else None
            for row, sign in ((before, -1), (after, 1)):
                if row is not None:
                    total = self.entries.get((row["caller_id"], row["day"]))
                    if total is not None:
                        total.add(row, sign)
        except GatewayError:
            # Admission refusals occur before allocation insertion. A rolled-back
            # request registration does not change any already checked total.
            raise
        except BaseException:
            # A failed transaction may have built a total, but cannot publish it.
            # Rebuild after failure, including failed commit or delta validation.
            self.entries.clear()
            raise
        finally:
            # Never advance data_version after releasing the write lock: a later
            # external commit must invalidate these totals at the next admission.
            self.revision = (version, db.total_changes)
