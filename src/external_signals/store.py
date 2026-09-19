"""Small durable store for external-signal experiments.

SQLite is intentional: this feature must run on an SBC without requiring the
optional control-plane Postgres stack. The database can later be exported to a
warehouse without losing the immutable raw scanner record.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schema import ExternalAnalysis, ExternalSignal


def _default_path() -> Path:
    raw = os.getenv("AIMM_EXTERNAL_SIGNAL_DB", ".runs/external_signals.sqlite3")
    return Path(raw)


class ExternalSignalStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init(self) -> None:
        with self._connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS external_signals (
                    signal_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    scanner_version TEXT NOT NULL,
                    scanner_score REAL,
                    scanner_grade TEXT,
                    signal_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_analyses (
                    signal_id TEXT PRIMARY KEY REFERENCES external_signals(signal_id),
                    status TEXT NOT NULL,
                    analysis_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_outcomes (
                    signal_id TEXT NOT NULL REFERENCES external_signals(signal_id),
                    horizon_name TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(signal_id, horizon_name)
                );
            """)

    def insert_signal(self, signal: ExternalSignal) -> tuple[bool, dict[str, Any] | None]:
        now = datetime.now(timezone.utc).isoformat()
        encoded = signal.model_dump_json()
        with self._connection() as con:
            found = con.execute(
                "SELECT signal_id FROM external_signals WHERE idempotency_key = ?",
                (signal.idempotency_key,),
            ).fetchone()
            if found:
                return False, self.get_signal(str(found["signal_id"]))
            con.execute(
                """INSERT INTO external_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(signal.signal_id), signal.idempotency_key, signal.source, signal.symbol,
                 signal.direction, signal.occurred_at.isoformat(), signal.submitted_at.isoformat(),
                 signal.scanner_version, signal.scanner_score, signal.scanner_grade, encoded, now),
            )
        return True, None

    def save_analysis(self, analysis: ExternalAnalysis) -> None:
        with self._connection() as con:
            con.execute(
                """INSERT INTO external_analyses(signal_id, status, analysis_json, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(signal_id) DO UPDATE SET status=excluded.status,
                   analysis_json=excluded.analysis_json, created_at=excluded.created_at""",
                (str(analysis.signal_id), analysis.status, analysis.model_dump_json(),
                 datetime.now(timezone.utc).isoformat()),
            )

    def save_outcome(self, signal_id: str, horizon_name: str, outcome: dict[str, Any]) -> None:
        with self._connection() as con:
            con.execute(
                """INSERT INTO external_outcomes VALUES (?, ?, ?, ?)
                   ON CONFLICT(signal_id, horizon_name) DO UPDATE SET
                   outcome_json=excluded.outcome_json, updated_at=excluded.updated_at""",
                (signal_id, horizon_name, json.dumps(outcome, default=str, sort_keys=True),
                 datetime.now(timezone.utc).isoformat()),
            )

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self._connection() as con:
            row = con.execute("SELECT * FROM external_signals WHERE signal_id = ?", (signal_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["signal"] = json.loads(result.pop("signal_json"))
            analysis = con.execute("SELECT analysis_json FROM external_analyses WHERE signal_id = ?", (signal_id,)).fetchone()
            result["analysis"] = json.loads(analysis["analysis_json"]) if analysis else None
            outcomes = con.execute("SELECT horizon_name, outcome_json FROM external_outcomes WHERE signal_id = ?", (signal_id,)).fetchall()
            result["outcomes"] = {r["horizon_name"]: json.loads(r["outcome_json"]) for r in outcomes}
            return result

