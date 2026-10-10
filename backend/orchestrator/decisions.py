"""Explainable decisions produced by each orchestrator cycle.

Every candidate that enters the engine leaves a record: the stage that
decided, the outcome, and the reasons that decided it. A decision that is
only visible as a missing row cannot be audited, so nothing is dropped
silently - including the session skip, the data-freshness gate, and the
interruption of a cycle that never completed.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

#: Stages a candidate passes through, in the order the engine applies them.
SESSION = "SESSION"
SCANNER = "SCANNER"
CANDIDATE = "CANDIDATE"
DATA_FRESHNESS = "DATA_FRESHNESS"
STRATEGY = "STRATEGY"
ADVISORY = "ADVISORY"
RISK = "RISK"
EXECUTION = "EXECUTION"
CYCLE = "CYCLE"

STAGES = (SESSION, SCANNER, CANDIDATE, DATA_FRESHNESS, STRATEGY, ADVISORY,
          RISK, EXECUTION, CYCLE)

APPROVED = "APPROVED"
REJECTED = "REJECTED"
SKIPPED = "SKIPPED"

OUTCOMES = (APPROVED, REJECTED, SKIPPED)

#: Journal event type every decision is persisted under, so a restart can
#: rebuild the ledger instead of losing the engine's history.
DECISION_EVENT = "DECISION"


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    cycle: int
    timestamp: datetime
    symbol: str | None
    stage: str
    outcome: str
    reasons: tuple[str, ...]
    context: dict | None = None

    def payload(self) -> dict:
        return {
            "cycle": self.cycle,
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "stage": self.stage,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "context": self.context,
        }

    @classmethod
    def from_journal(cls, row: dict) -> "DecisionRecord | None":
        """Rebuild a record from its journaled row, or None if unusable."""
        context = row.get("context") or row.get("context_json") or {}
        if not isinstance(context, dict):
            return None
        stage = str(context.get("stage") or "")
        outcome = str(context.get("outcome") or "")
        if stage not in STAGES or outcome not in OUTCOMES:
            return None
        timestamp = row.get("timestamp")
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                return None
        if not isinstance(timestamp, datetime):
            return None
        try:
            cycle = int(context.get("cycle") or 0)
        except (TypeError, ValueError):
            return None
        reasons = context.get("reasons") or ()
        if isinstance(reasons, str):
            reasons = (reasons,)
        return cls(
            cycle=cycle, timestamp=timestamp,
            symbol=(str(row.get("symbol") or "").strip().upper() or None),
            stage=stage, outcome=outcome,
            reasons=tuple(str(reason) for reason in reasons),
            context=None,
        )


class DecisionLedger:
    """Bounded, newest-last record of every decision the engine made.

    The ledger is a live view, not the source of truth: each record is also
    journaled under one event type, so the history survives a restart and
    :meth:`restore` can rebuild it.
    """

    def __init__(self, *, capacity: int = 500):
        if capacity < 1:
            raise ValueError("decision ledger capacity must be positive")
        self._records: deque[DecisionRecord] = deque(maxlen=capacity)
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._records.maxlen

    def record(self, cycle: int, timestamp: datetime, symbol: str | None,
               stage: str, outcome: str, reasons, context: dict | None = None) -> DecisionRecord:
        if stage not in STAGES:
            raise ValueError(f"unknown decision stage: {stage}")
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown decision outcome: {outcome}")
        decision = DecisionRecord(
            cycle=cycle, timestamp=timestamp,
            symbol=symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else None,
            stage=stage, outcome=outcome,
            reasons=tuple(str(reason) for reason in reasons or ()),
            context=dict(context) if context else None,
        )
        with self._lock:
            self._records.append(decision)
        return decision

    def recent(self, limit: int = 100) -> tuple[DecisionRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            records = list(self._records)
        return tuple(records[-limit:])

    def restore(self, records) -> int:
        """Re-seed the ledger from journal rows after a restart.

        The journal returns rows newest first; the ledger is chronological,
        so the rows are reversed and then stably sorted by timestamp - a
        cycle's decisions keep the order the engine made them in.
        """
        restored: list[DecisionRecord] = []
        for row in records:
            decision = (row if isinstance(row, DecisionRecord)
                        else DecisionRecord.from_journal(row) if isinstance(row, dict)
                        else None)
            if decision is not None:
                restored.append(decision)
        restored.reverse()
        restored.sort(key=lambda item: (item.timestamp, item.cycle))
        with self._lock:
            self._records.clear()
            kept = restored[-self._records.maxlen:]
            for decision in kept:
                self._records.append(decision)
            return len(kept)

    def summary(self) -> dict:
        with self._lock:
            records = list(self._records)
        by_stage: dict[str, int] = {}
        approved = rejected = skipped = 0
        for record in records:
            by_stage[record.stage] = by_stage.get(record.stage, 0) + 1
            if record.outcome is APPROVED:
                approved += 1
            elif record.outcome is REJECTED:
                rejected += 1
            else:
                skipped += 1
        latest = records[-1] if records else None
        return {
            "total": len(records),
            "approved": approved,
            "rejected": rejected,
            "skipped": skipped,
            "by_stage": by_stage,
            "latest_cycle": latest.cycle if latest else 0,
            "latest": latest.payload() if latest else None,
            "capacity": self._records.maxlen,
        }
