# -*- coding: utf-8 -*-
"""Schemas for the rule-based steady-income module."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SteadyIncomeReplayPeriod(BaseModel):
    label: str
    start_date: str
    end_date: str
    adjusted_price_return_pct: float


class SteadyIncomeHistoryCoverage(BaseModel):
    year: int
    history_start: Optional[str] = None
    history_end: Optional[str] = None
    actual_sessions: int = 0
    expected_sessions: int = 0
    coverage_ratio: float = 0.0
    complete: bool = False


class SteadyIncomeCandidate(BaseModel):
    schema_version: int
    model_version: str
    ruleset_version: str
    evaluator_version: str
    sector_model_version: str
    evidence_version: str
    price_model_version: str
    code: str
    sector_model: Literal[
        "normal_corporate",
        "bank",
        "insurer",
        "broker",
        "unsupported_financial",
        "unknown",
    ]
    industry: Optional[str] = None
    risk_tier: Literal["稳健", "较稳健", "观察", "不纳入", "数据不足"]
    public_risk_label: Literal["规则低风险 A", "规则低风险 B", "规则观察", "规则排除", "数据不足"]
    qualified: bool
    ranking_score: Optional[int] = Field(None, ge=0, le=100)
    score: Optional[int] = Field(None, ge=0, le=100, description="Deprecated alias of ranking_score")
    score_deprecated: bool = True
    current_price: Optional[float] = None
    price_date: Optional[str] = None
    ttm_dividend_yield_pct: Optional[float] = None
    ttm_cash_dividend_per_share: Optional[float] = None
    consecutive_dividend_years: int = 0
    dividend_sustainability: str
    cash_flow_coverage_ratio: Optional[float] = None
    roe_pct: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    annualized_volatility_pct: Optional[float] = None
    positive_replay_periods: int = 0
    replay_periods: List[SteadyIncomeReplayPeriod] = Field(default_factory=list)
    history_coverage: List[SteadyIncomeHistoryCoverage] = Field(default_factory=list)
    price_adjustment: str = "unknown"
    strengths: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    data_status: Literal["完整", "部分数据", "数据不足"]
    failure_code: str = "none"
    terminal_status: Literal[
        "evaluated_qualified",
        "evaluated_rejected",
        "insufficient_evidence",
        "unsupported_sector_model",
        "provider_failure",
        "internal_error",
    ] = "insufficient_evidence"
    evidence_issues: List[str] = Field(default_factory=list)
    evidence_status: Dict[str, str] = Field(default_factory=dict)
    provider_diagnostics: List[Dict[str, Any]] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    data_notes: List[str] = Field(default_factory=list)


class SteadyIncomeResponse(BaseModel):
    schema_version: int
    model_version: str
    ruleset_version: str
    evaluator_version: str
    sector_model_version: str
    evidence_version: str
    price_model_version: str
    generated_at: str
    as_of: str
    source: str
    data_status: Literal[
        "complete",
        "degraded",
        "valid_zero",
        "partial",
        "provider_unavailable",
        "source_schema_changed",
    ]
    selection_mode: Literal["portfolio", "fixed_shortlist", "adaptive_shortlist", "exhaustive"] = "portfolio"
    universe_count: int = 0
    prefilter_count: int = 0
    deep_budget: int = 0
    deep_requested_count: int = 0
    deep_attempted_count: int = 0
    deep_completed_count: int = 0
    deep_evaluated_count: int = Field(0, description="Deprecated alias of deep_completed_count")
    unevaluated_count: int = 0
    is_exhaustive: bool = True
    evaluated_count: int = Field(0, description="Deprecated alias of deep_completed_count")
    qualified_count: int
    rejected_count: int = 0
    insufficient_evidence_count: int = 0
    unsupported_sector_count: int = 0
    provider_failure_count: int = 0
    internal_error_count: int = 0
    terminal_status_distribution: Dict[str, int] = Field(default_factory=dict)
    candidates: List[SteadyIncomeCandidate] = Field(default_factory=list)
    excluded: List[SteadyIncomeCandidate] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    methodology: Dict[str, str] = Field(default_factory=dict)
    screening_stats: Dict[str, Any] = Field(default_factory=dict)
