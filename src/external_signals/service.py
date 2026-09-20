"""Analysis-only LangGraph adapter for submitted quantitative scanner signals."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, StateGraph

from .schema import ExternalAnalysis, ExternalSignal
from schemas.state import HedgeFundState

_DESKS = {
    "breakout": ["pattern_recognition_bot", "technical_ta_engine", "liquidity_order_flow"],
    "funding": ["statistical_alpha_engine", "technical_ta_engine", "liquidity_order_flow"],
}


def _submitted_ticker_context(state: dict[str, Any]) -> dict[str, Any]:
    """Fetch only the submitted asset's live context; never scan a trade universe."""
    from agents.market_scan import MarketScanAgent

    ticker = str(state.get("ticker") or "")
    market_data = dict(state.get("market_data") or {})
    try:
        market_data[ticker] = MarketScanAgent(testnet=True).fetch_data(ticker)
    except Exception as exc:
        market_data[ticker] = {"status": "error", "error": str(exc)}
    return {"market_data": market_data}


def selected_desks(source: str) -> list[str]:
    return list(_DESKS[source])


def _is_stale(signal: ExternalSignal) -> bool:
    seconds = int(os.getenv("AIMM_EXTERNAL_SIGNAL_MAX_AGE_SECONDS", "900"))
    return (datetime.now(timezone.utc) - signal.occurred_at).total_seconds() > max(1, seconds)


def _build_analysis_graph(source: str):
    # Importing main lazily preserves normal API/startup behavior and avoids a cycle.
    from main import (
        _tier0_node_fns,
        desk_debate,
        policy_orchestrator,
        risk,
        risk_guard,
    )
    from workflow.weighted_arbitrator import weighted_arbitrator_node

    desks = selected_desks(source)
    functions = _tier0_node_fns()
    # HedgeFundState declares append reducers for the parallel desk contracts,
    # market context, and reasoning logs. A bare dict silently loses those
    # concurrent updates and produces an empty, misleading "completed" result.
    graph = StateGraph(HedgeFundState)
    graph.add_node("policy", policy_orchestrator)
    graph.add_node("submitted_ticker_context", _submitted_ticker_context)
    for desk in desks:
        graph.add_node(desk, functions[desk])
    graph.add_node("risk", risk)
    graph.add_node("debate", desk_debate)
    graph.add_node("arbitrator", weighted_arbitrator_node)
    graph.add_node("risk_guard", risk_guard)
    graph.set_entry_point("policy")
    graph.add_edge("policy", "submitted_ticker_context")
    for desk in desks:
        graph.add_edge("submitted_ticker_context", desk)
        graph.add_edge(desk, "risk")
    graph.add_edge("risk", "debate")
    graph.add_edge("debate", "arbitrator")
    graph.add_edge("arbitrator", "risk_guard")
    graph.add_edge("risk_guard", END)
    return graph.compile()


def _signal_ticker(signal: ExternalSignal) -> str:
    # Scanner symbols are Bybit-style (BTCUSDT); the upstream graph uses CCXT pairs.
    raw = signal.symbol.upper().replace("/", "").replace(":", "")
    return f"{raw[:-4]}/USDT" if raw.endswith("USDT") else signal.symbol


def _analysis_from_state(signal: ExternalSignal, state: dict[str, Any], *, stale: bool) -> ExternalAnalysis:
    proposal = state.get("proposed_signal") if isinstance(state.get("proposed_signal"), dict) else {}
    params = proposal.get("params") if isinstance(proposal.get("params"), dict) else {}
    risk = state.get("risk_guard") if isinstance(state.get("risk_guard"), dict) else {}
    reasons = [str(x) for x in params.get("reasons", []) if x]
    stance = str(params.get("stance") or "neutral").lower()
    if stance not in {"bullish", "bearish", "neutral"}:
        stance = "neutral"
    confidence = float(params.get("confidence") or 0.0)
    vetoed = bool(state.get("is_vetoed"))
    agent_outputs = {name: state.get(name, {}) for name in selected_desks(signal.source)}
    risks = [str(state.get("veto_reason") or "")]
    if stale:
        risks.append("stale_signal")
    risk_extra = ((risk.get("reasoning") or {}).get("extra") if isinstance(risk.get("reasoning"), dict) else {})
    risks.extend(str(x) for x in (risk_extra.get("reasons", []) if isinstance(risk_extra, dict) else []))
    has_desk_output = any(
        isinstance(output, dict) and bool(output)
        for output in agent_outputs.values()
    )
    has_arbitration = bool(params)
    has_risk_guard = bool(risk)
    if not (has_desk_output and has_arbitration and has_risk_guard):
        missing = []
        if not has_desk_output:
            missing.append("desk_outputs")
        if not has_arbitration:
            missing.append("arbitration")
        if not has_risk_guard:
            missing.append("risk_guard")
        return ExternalAnalysis(
            signal_id=signal.signal_id,
            status="failed",
            deserves_further_consideration=False,
            confidence=0.0,
            stance="neutral",
            risks=["incomplete_analysis_workflow", *risks][:8],
            invalidation_conditions=["Complete desk, arbitration, and Risk Guard output is required"],
            contributing_components=selected_desks(signal.source) + ["weighted_arbitrator", "risk_guard"],
            agent_outputs=agent_outputs,
            risk_decision=risk,
            human_reasoning=f"Incomplete analysis workflow; missing: {', '.join(missing)}.",
        )
    invalidations = []
    if vetoed:
        invalidations.append("Risk Guard veto")
    if stale:
        invalidations.append("Signal older than configured maximum age")
    return ExternalAnalysis(
        signal_id=signal.signal_id,
        status="completed",
        deserves_further_consideration=not stale and not vetoed and stance != "neutral",
        confidence=max(0.0, min(1.0, confidence)),
        stance=stance,
        supporting_evidence=reasons[:8],
        contradictory_evidence=["risk_guard_veto"] if vetoed else [],
        risks=[x for x in risks if x][:8],
        invalidation_conditions=invalidations,
        contributing_components=selected_desks(signal.source) + ["weighted_arbitrator", "risk_guard"],
        agent_outputs=agent_outputs,
        risk_decision=risk,
        # This endpoint never enables execution or writes to the paper account.
        execution_permitted=False,
        paper_trade_eligible=False,
        paper_trade_reason="analysis_only_requires_explicit_experiment_policy",
        human_reasoning="; ".join(reasons[:5]),
    )


def analyze(signal: ExternalSignal) -> ExternalAnalysis:
    stale = _is_stale(signal)
    if stale:
        return ExternalAnalysis(
            signal_id=signal.signal_id, status="rejected", deserves_further_consideration=False,
            confidence=0.0, stance="neutral", risks=["stale_signal"],
            invalidation_conditions=["Signal older than configured maximum age"],
            contributing_components=["stale_signal_guard"], risk_decision={"status": "VETOED"},
            human_reasoning="The external signal was rejected before model analysis because it is stale.",
        )
    from schemas.state import initial_hedge_fund_state

    state = initial_hedge_fund_state(run_mode="paper", ticker=_signal_ticker(signal))
    state["shared_memory"] = {
        "external_signal": signal.model_dump(mode="json"),
        "external_signal_mode": "analysis_only",
    }
    state["arbitrator_mode"] = "agent_llm" if os.getenv("AIMM_EXTERNAL_SIGNAL_LLM") == "1" else "weighted_convergence"
    state["llm_enabled_agents"] = selected_desks(signal.source)
    output = _build_analysis_graph(signal.source).invoke(state)
    return _analysis_from_state(signal, dict(output), stale=False)

