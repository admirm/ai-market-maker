from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from external_signals.schema import ExternalAnalysis, ExternalSignal
from external_signals.service import _analysis_from_state, selected_desks
from external_signals.store import ExternalSignalStore


def _signal() -> ExternalSignal:
    return ExternalSignal(
        signal_id=uuid4(), source="funding", occurred_at=datetime.now(timezone.utc),
        symbol="BTCUSDT", direction="long", scanner_version="8.17", scanner_score=12,
        market={"price": 100.0, "exchange": "BB"}, payload={"setup_type": "SHORT SQZ"},
        raw_signal={"score": 12}, idempotency_key="external-test-key-0001",
    )


def test_source_selects_relevant_desks() -> None:
    assert selected_desks("breakout") == ["pattern_recognition_bot", "technical_ta_engine", "liquidity_order_flow"]
    assert selected_desks("funding") == ["statistical_alpha_engine", "technical_ta_engine", "liquidity_order_flow"]


def test_store_is_idempotent_and_retains_outcome(tmp_path: Path) -> None:
    store = ExternalSignalStore(tmp_path / "signals.sqlite3")
    signal = _signal()
    inserted, existing = store.insert_signal(signal)
    assert inserted and existing is None
    inserted, existing = store.insert_signal(signal)
    assert not inserted and existing is not None
    analysis = ExternalAnalysis(signal_id=signal.signal_id, status="completed", deserves_further_consideration=False,
                                confidence=0.1, stance="neutral")
    store.save_analysis(analysis)
    store.save_outcome(str(signal.signal_id), "30m", {"status": "complete", "signed_return_pct": 0.01})
    row = store.get_signal(str(signal.signal_id))
    assert row and row["analysis"]["status"] == "completed"
    assert row["outcomes"]["30m"]["signed_return_pct"] == 0.01


def test_empty_workflow_state_fails_closed() -> None:
    analysis = _analysis_from_state(_signal(), {}, stale=False)
    assert analysis.status == "failed"
    assert analysis.deserves_further_consideration is False
    assert "incomplete_analysis_workflow" in analysis.risks


