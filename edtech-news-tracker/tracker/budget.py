"""
Hard spend cap for Parallel.ai usage.

Every billable call (search / extract) must `reserve()` its estimated cost
BEFORE it is made; if the reservation would push total spend past the cap the
call is skipped. A failed call is `refund()`ed. The running total is persisted
so an interrupted/resumed run (same date window) keeps counting against the
same budget and can never exceed it.

Costs are ESTIMATES from per-request prices configured in config.py (check
your Parallel.ai plan/dashboard and override via env if they differ).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class BudgetTracker:
    def __init__(self, cap_usd: float, ledger_path: Path | None = None):
        self.cap = cap_usd
        self._path = ledger_path
        self._lock = threading.Lock()
        self.spent = 0.0
        self.searches = 0
        self.extracts = 0
        self.skipped = 0
        if ledger_path and ledger_path.exists():
            try:
                d = json.loads(ledger_path.read_text())
                self.spent = float(d.get("spent", 0.0))
                self.searches = int(d.get("searches", 0))
                self.extracts = int(d.get("extracts", 0))
            except Exception:  # noqa: BLE001 - corrupt ledger: treat conservatively below
                self.spent = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.cap - self.spent)

    def reserve(self, cost: float, kind: str = "search", count: int = 1) -> bool:
        with self._lock:
            if self.spent + cost > self.cap + 1e-9:
                self.skipped += count
                return False
            self.spent += cost
            if kind == "search":
                self.searches += count
            else:
                self.extracts += count
            self._persist()
            return True

    def refund(self, cost: float, kind: str = "search", count: int = 1) -> None:
        with self._lock:
            self.spent = max(0.0, self.spent - cost)
            if kind == "search":
                self.searches = max(0, self.searches - count)
            else:
                self.extracts = max(0, self.extracts - count)
            self._persist()

    def can_afford(self, cost: float) -> bool:
        with self._lock:
            return self.spent + cost <= self.cap + 1e-9

    def _persist(self) -> None:
        if self._path:
            self._path.write_text(
                json.dumps({"spent": round(self.spent, 6), "searches": self.searches, "extracts": self.extracts})
            )

    def summary(self) -> str:
        return (
            f"~${self.spent:.2f} of ${self.cap:.2f} budget used "
            f"({self.searches} searches, {self.extracts} URL extractions, {self.skipped} calls skipped by cap)"
        )
