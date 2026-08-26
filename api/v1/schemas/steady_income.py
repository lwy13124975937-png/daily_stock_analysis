# -*- coding: utf-8 -*-
"""Schemas for the rule-based steady-income module."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SteadyIncomeReplayPeriod(BaseModel):
    label: str
    start_date: str
    end_date: str
    total_return_pct: float


class SteadyIncomePriceBands(BaseModel):
    high_income_price: float
    balanced_price: float
    low_income_price: float


class SteadyIncomeCandidate(BaseModel):
    code: str
    risk_tier: str
    qualified: bool
    score: int = Field(..., ge=0, le=100)
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
    price_bands: Optional[SteadyIncomePriceBands] = None
    strengths: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    data_status: str
    data_notes: List[str] = Field(default_factory=list)


class SteadyIncomeResponse(BaseModel):
    generated_at: str
    as_of: str
    source: str
    evaluated_count: int
    qualified_count: int
    candidates: List[SteadyIncomeCandidate] = Field(default_factory=list)
    excluded: List[SteadyIncomeCandidate] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    methodology: Dict[str, str] = Field(default_factory=dict)
