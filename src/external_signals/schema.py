"""Stable contract for scanner-originated quantitative evidence.

The envelope deliberately keeps source-specific features under ``payload`` while
preserving the raw scanner row for audit and later outcome/ML work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "external-signal/v1"


class MarketContext(BaseModel):
    model_config = ConfigDict(extra="allow")
    price: float = Field(gt=0)
    exchange: str = Field(min_length=1, max_length=80)
    volume_24h: float | None = Field(default=None, ge=0)
    open_interest: float | None = Field(default=None, ge=0)
    funding_rate: float | None = None
    regime: str | None = Field(default=None, max_length=80)


class ExternalSignal(BaseModel):
    """A scanner observation, never an execution instruction."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    signal_id: UUID
    source: Literal["breakout", "funding"]
    occurred_at: datetime
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    symbol: str = Field(min_length=2, max_length=80)
    direction: Literal["long", "short"]
    scanner_version: str = Field(min_length=1, max_length=40)
    scanner_score: float | None = None
    scanner_grade: str | None = Field(default=None, max_length=20)
    market: MarketContext
    payload: dict[str, Any] = Field(default_factory=dict)
    raw_signal: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("occurred_at", "submitted_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a UTC offset")
        return value.astimezone(timezone.utc)


class ExternalAnalysis(BaseModel):
    """Machine-facing response; prose is supplemental audit evidence only."""

    model_config = ConfigDict(extra="forbid")
    signal_id: UUID
    status: Literal["completed", "rejected", "failed"]
    deserves_further_consideration: bool
    confidence: float = Field(ge=0, le=1)
    stance: Literal["bullish", "bearish", "neutral"]
    supporting_evidence: list[str] = Field(default_factory=list)
    contradictory_evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    contributing_components: list[str] = Field(default_factory=list)
    agent_outputs: dict[str, Any] = Field(default_factory=dict)
    risk_decision: dict[str, Any] = Field(default_factory=dict)
    execution_permitted: Literal[False] = False
    paper_trade_eligible: bool = False
    paper_trade_reason: str = "analysis_only"
    human_reasoning: str = ""

