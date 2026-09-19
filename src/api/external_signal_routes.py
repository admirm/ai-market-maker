"""Authenticated intake API for independently generated scanner signals."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from external_signals.schema import ExternalSignal
from external_signals.service import analyze
from external_signals.store import ExternalSignalStore

router = APIRouter(prefix="/external-signals", tags=["external-signals"])


class OutcomeUpdate(BaseModel):
    horizon_name: str = Field(min_length=1, max_length=80)
    outcome: dict[str, Any]


@router.post("")
def submit_external_signal(signal: ExternalSignal) -> dict:
    """Persist and analyze a scanner signal; idempotency is keyed by the envelope."""
    store = ExternalSignalStore()
    inserted, existing = store.insert_signal(signal)
    if not inserted:
        return {"ok": True, "duplicate": True, "record": existing}
    try:
        result = analyze(signal)
    except Exception as exc:  # keep raw evidence even when an agent/provider is unavailable
        from external_signals.schema import ExternalAnalysis
        result = ExternalAnalysis(
            signal_id=signal.signal_id, status="failed", deserves_further_consideration=False,
            confidence=0.0, stance="neutral", risks=["analysis_failure"],
            invalidation_conditions=["A complete analysis is required before consideration"],
            contributing_components=[], risk_decision={"status": "VETOED", "detail": str(exc)},
            human_reasoning=f"Analysis failed safely: {type(exc).__name__}",
        )
    store.save_analysis(result)
    return {"ok": True, "duplicate": False, "analysis": result.model_dump(mode="json")}


@router.get("/{signal_id}")
def get_external_signal(signal_id: str) -> dict:
    record = ExternalSignalStore().get_signal(signal_id)
    if record is None:
        raise HTTPException(status_code=404, detail="external signal not found")
    return record


@router.put("/{signal_id}/outcomes")
def put_external_outcome(signal_id: str, update: OutcomeUpdate) -> dict:
    store = ExternalSignalStore()
    if store.get_signal(signal_id) is None:
        raise HTTPException(status_code=404, detail="external signal not found")
    store.save_outcome(signal_id, update.horizon_name, update.outcome)
    return {"ok": True, "signal_id": signal_id, "horizon_name": update.horizon_name}


