# -*- coding: utf-8 -*-
"""Rule-based low-risk steady-income evaluation and current-holdings service.

The module deliberately avoids LLM calls.  It ranks only inside hard risk
tiers so an attractive dividend yield cannot hide weak cash flow or excessive
drawdown.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from src.core.session_calendar import ExchangeSessionCalendar, SessionCalendar, SessionCalendarUnavailable
from src.services.steady_income_contracts import (
    STEADY_INCOME_EVALUATOR_VERSION,
    STEADY_INCOME_EVIDENCE_VERSION,
    STEADY_INCOME_MODEL_VERSION,
    STEADY_INCOME_PRICE_MODEL_VERSION,
    STEADY_INCOME_RULESET_VERSION,
    STEADY_INCOME_SCHEMA_VERSION,
    STEADY_INCOME_SECTOR_MODEL_VERSION,
    VERSION_FINGERPRINT,
    SectorModel,
    SteadyTerminalStatus,
    public_risk_label,
    resolve_sector_model,
    summarize_deep_evaluation_counts,
)


RISK_TIER_ORDER = {
    "稳健": 0,
    "较稳健": 1,
    "观察": 2,
    "不纳入": 3,
    "数据不足": 4,
}
QUALIFIED_TIERS = {"稳健", "较稳健"}
CACHE_TTL_SECONDS = 6 * 60 * 60
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
HISTORY_YEAR_MIN_COVERAGE = 0.95
A_SHARE_EQUITY_PREFIXES = (
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "600",
    "601",
    "603",
    "605",
    "688",
    "689",
)


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_a_share_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    for prefix in ("SH.", "SZ.", "BJ.", "SH", "SZ", "BJ"):
        if code.startswith(prefix) and code[len(prefix):].isdigit():
            code = code[len(prefix):]
            break
    if "." in code:
        base, suffix = code.rsplit(".", 1)
        if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
            code = base
    return code if len(code) == 6 and code.isdigit() else ""


def _is_a_share_equity_code(code: str) -> bool:
    """Keep listed A-share equities while excluding funds, bonds, and indices."""
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(A_SHARE_EQUITY_PREFIXES) or code[0] in {"4", "8"} or code.startswith("920")


def _block_data(context: Dict[str, Any], key: str) -> Dict[str, Any]:
    block = context.get(key)
    if not isinstance(block, dict):
        return {}
    data = block.get("data")
    return data if isinstance(data, dict) else block


def _consecutive_dividend_years(events: Iterable[Dict[str, Any]], as_of: date) -> int:
    years = set()
    for event in events:
        cash_dividend = _safe_float(event.get("cash_dividend_per_share"))
        if cash_dividend is None or cash_dividend <= 0:
            continue
        raw_date = event.get("event_date") or event.get("ex_dividend_date")
        try:
            event_date = date.fromisoformat(str(raw_date)[:10])
        except (TypeError, ValueError):
            continue
        if event_date <= as_of:
            years.add(event_date.year)
    if not years:
        return 0

    latest = max(years)
    if latest < as_of.year - 1:
        return 0
    streak = 0
    cursor = latest
    while cursor in years:
        streak += 1
        cursor -= 1
    return streak


def _normalize_history(frame: Any) -> pd.DataFrame:
    empty_history = pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "close": pd.Series(dtype="float64"),
        }
    )
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return empty_history
    if "date" not in frame.columns or "close" not in frame.columns:
        return empty_history
    work = frame[["date", "close"]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    normalized = (
        work.dropna(subset=["date", "close"])
        .loc[lambda item: item["close"] > 0]
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
    if normalized.empty:
        return empty_history
    return normalized


def _history_metrics(
    frame: Any,
    as_of: date,
    *,
    calendar: SessionCalendar | None = None,
    price_adjustment: str = "unknown",
) -> Dict[str, Any]:
    """Compute price risk and year coverage against official sessions."""

    history = _normalize_history(frame)
    history = history.loc[history["date"].dt.date <= as_of].reset_index(drop=True)
    empty_result = {
        "annualized_volatility_pct": None,
        "max_drawdown_pct": None,
        "replay_periods": [],
        "positive_replay_periods": 0,
        "history_coverage": [],
        "price_adjustment": price_adjustment or "unknown",
        "calendar_status": "available",
    }
    if len(history) < 2:
        return empty_result

    closes = history["close"]
    returns = closes.pct_change().dropna()
    volatility = None
    if len(returns) >= 20:
        volatility = float(returns.std(ddof=0) * math.sqrt(252) * 100.0)
    drawdown = closes / closes.cummax() - 1.0
    max_drawdown = abs(float(drawdown.min() * 100.0)) if not drawdown.empty else None

    session_calendar = calendar
    if session_calendar is None:
        try:
            session_calendar = ExchangeSessionCalendar()
        except SessionCalendarUnavailable:
            result = dict(empty_result)
            result.update(
                {
                    "annualized_volatility_pct": round(volatility, 2) if volatility is not None else None,
                    "max_drawdown_pct": round(max_drawdown, 2) if max_drawdown is not None else None,
                    "calendar_status": "unavailable",
                }
            )
            return result

    complete_years = history.loc[history["date"].dt.year < as_of.year].copy()
    year_ends: List[Dict[str, Any]] = []
    coverage_rows: List[Dict[str, Any]] = []
    for year, group in complete_years.groupby(complete_years["date"].dt.year):
        expected = list(session_calendar.sessions_between(date(int(year), 1, 1), date(int(year), 12, 31)))
        actual_dates = sorted(set(group["date"].dt.date))
        actual_sessions = len(set(expected).intersection(actual_dates))
        expected_sessions = len(expected)
        coverage_ratio = actual_sessions / expected_sessions if expected_sessions else 0.0
        boundary_ok = bool(
            expected
            and actual_dates
            and actual_dates[0] <= expected[min(4, len(expected) - 1)]
            and actual_dates[-1] >= expected[max(0, len(expected) - 5)]
        )
        complete = bool(expected_sessions and coverage_ratio >= HISTORY_YEAR_MIN_COVERAGE and boundary_ok)
        coverage_rows.append(
            {
                "year": int(year),
                "history_start": actual_dates[0].isoformat() if actual_dates else None,
                "history_end": actual_dates[-1].isoformat() if actual_dates else None,
                "actual_sessions": actual_sessions,
                "expected_sessions": expected_sessions,
                "coverage_ratio": round(coverage_ratio, 4),
                "complete": complete,
            }
        )
        if not complete:
            continue
        year_ends.append(
            {
                "year": int(year),
                "date": group.iloc[-1]["date"].date(),
                "close": float(group.iloc[-1]["close"]),
            }
        )

    replay_periods: List[Dict[str, Any]] = []
    for previous, current in zip(year_ends, year_ends[1:]):
        if current["year"] != previous["year"] + 1:
            continue
        period_return = (current["close"] / previous["close"] - 1.0) * 100.0
        replay_periods.append(
            {
                "label": str(current["year"]),
                "start_date": previous["date"].isoformat(),
                "end_date": current["date"].isoformat(),
                "adjusted_price_return_pct": round(period_return, 2),
            }
        )
    replay_periods = replay_periods[-5:]
    return {
        "annualized_volatility_pct": round(volatility, 2) if volatility is not None else None,
        "max_drawdown_pct": round(max_drawdown, 2) if max_drawdown is not None else None,
        "replay_periods": replay_periods,
        "positive_replay_periods": sum(
            1 for item in replay_periods if item["adjusted_price_return_pct"] > 0
        ),
        "history_coverage": coverage_rows,
        "price_adjustment": price_adjustment or "unknown",
        "calendar_status": "available",
    }


def _yield_score(dividend_yield: Optional[float]) -> int:
    if dividend_yield is None or dividend_yield <= 0:
        return 0
    if 3.0 <= dividend_yield <= 6.0:
        return 20
    if 2.0 <= dividend_yield < 3.0:
        return 14
    if 6.0 < dividend_yield <= 8.0:
        return 12
    if 1.0 <= dividend_yield < 2.0:
        return 7
    return 3


def _parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _implemented_dividend_events(events: Iterable[Dict[str, Any]], as_of: date) -> list[Dict[str, Any]]:
    result: list[Dict[str, Any]] = []
    selected: dict[str, Dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        status = str(event.get("implementation_status") or "").strip().lower()
        implemented = event.get("implemented") is True or status == "implemented"
        event_date = _parse_iso_date(event.get("ex_dividend_date") or event.get("event_date"))
        cash = _safe_float(event.get("cash_dividend_per_share"))
        if not implemented or event_date is None or event_date > as_of or cash is None or cash <= 0:
            continue
        normalized = dict(event)
        normalized["event_date"] = event_date.isoformat()
        normalized["ex_dividend_date"] = event_date.isoformat()
        normalized["cash_dividend_per_share"] = cash
        identity = event_date.isoformat()
        previous = selected.get(identity)
        if previous is None or str(normalized.get("announcement_date") or "") >= str(
            previous.get("announcement_date") or ""
        ):
            selected[identity] = normalized
    result.extend(selected.values())
    return sorted(result, key=lambda item: str(item.get("event_date") or ""))


def _financial_flows_are_comparable(financial: Dict[str, Any]) -> bool:
    profit_period = _parse_iso_date(financial.get("net_profit_period_end"))
    cash_period = _parse_iso_date(financial.get("operating_cash_flow_period_end"))
    profit_unit = str(financial.get("net_profit_unit") or "").strip()
    cash_unit = str(financial.get("operating_cash_flow_unit") or "").strip()
    profit_basis = str(financial.get("net_profit_flow_basis") or "").strip().lower()
    cash_basis = str(financial.get("operating_cash_flow_flow_basis") or "").strip().lower()
    return bool(
        profit_period
        and cash_period
        and profit_period == cash_period
        and profit_unit
        and cash_unit
        and profit_unit == cash_unit
        and profit_basis
        and cash_basis
        and profit_basis == cash_basis
    )


def _provider_cash_flow_coverage(financial: Dict[str, Any]) -> float | None:
    """Return an explicit same-period ratio supplied by the financial provider.

    This is deliberately separate from the raw-flow calculation.  Some public
    provider responses expose raw amounts without a machine-verifiable unit or
    period basis, but also expose their own dimensionless same-column ratio.
    Using that ratio avoids guessing a currency unit or mixing flow periods.
    """

    ratio = _safe_float(financial.get("cash_flow_coverage_ratio"))
    ratio_period = _parse_iso_date(financial.get("cash_flow_coverage_period_end"))
    report_period = _parse_iso_date(financial.get("period_end") or financial.get("report_date"))
    source = str(financial.get("cash_flow_coverage_source") or "").strip()
    if ratio is None or ratio_period is None or report_period is None:
        return None
    if ratio_period != report_period or source != "provider_reported_same_period_ratio":
        return None
    return ratio


_EVIDENCE_ISSUE_CODES = {
    "当前价格": "missing_current_price",
    "行情日期": "missing_price_date",
    "行情日期晚于评估日": "future_price_date",
    "行情日期早于最近应有交易日": "stale_price_date",
    "TTM 股息率": "missing_ttm_dividend_yield",
    "TTM 每股现金分红": "missing_ttm_cash_dividend",
    "现金分红记录": "missing_implemented_dividend_history",
    "长期行情": "missing_price_history",
    "三年以上完整年度行情": "insufficient_history_coverage",
    "财务报告期间": "missing_financial_period",
    "财务披露时点证据": "missing_available_at",
    "同期间利润/经营现金流": "missing_financial_flows",
    "财务流量期间/口径/单位一致性": "unverifiable_financial_flow_semantics",
    "金融行业专用监管指标": "missing_regulatory_metrics",
    "标准行业分类": "missing_canonical_sector",
    "交易日历": "trading_calendar_unavailable",
}


def _context_industry(context: Dict[str, Any]) -> str:
    master = context.get("security_master")
    if isinstance(master, dict) and master.get("industry"):
        return str(master["industry"]).strip()
    for key in ("profile", "company_profile", "basic_info"):
        block = _block_data(context, key)
        if block.get("industry"):
            return str(block["industry"]).strip()
    return ""


def evaluate_steady_income_candidate(
    *,
    code: str,
    current_price: Optional[float],
    price_date: Optional[str],
    context: Dict[str, Any],
    history: Any,
    as_of: date,
    sector_model: SectorModel | str | None = None,
    mode: str = "live",
    calendar: SessionCalendar | None = None,
    price_adjustment: str = "unknown",
    evaluation_moment: datetime | None = None,
) -> Dict[str, Any]:
    """Evaluate one stock using versioned, fail-closed evidence rules."""

    if mode not in {"live", "historical"}:
        raise ValueError("mode must be 'live' or 'historical'")

    growth = _block_data(context, "growth")
    valuation = _block_data(context, "valuation")
    earnings = _block_data(context, "earnings")
    dividend = earnings.get("dividend") if isinstance(earnings.get("dividend"), dict) else {}
    financial = earnings.get("financial_report") if isinstance(earnings.get("financial_report"), dict) else {}
    raw_events = dividend.get("events") if isinstance(dividend.get("events"), list) else []
    events = _implemented_dividend_events(raw_events, as_of)
    industry = _context_industry(context)
    resolved_sector = SectorModel(sector_model) if sector_model else resolve_sector_model(industry)

    ttm_start = as_of - timedelta(days=365)
    ttm_events = [
        event
        for event in events
        if ttm_start <= date.fromisoformat(str(event["event_date"])[:10]) <= as_of
    ]
    ttm_cash = (
        sum(float(event["cash_dividend_per_share"]) for event in ttm_events)
        if ttm_events
        else None
    )
    normalized_price = _safe_float(current_price)
    if normalized_price is not None and normalized_price <= 0:
        normalized_price = None
    ttm_yield = None
    if ttm_cash is not None and ttm_cash > 0 and normalized_price is not None:
        ttm_yield = ttm_cash / normalized_price * 100.0
    streak = _consecutive_dividend_years(events, as_of)
    net_profit = _safe_float(financial.get("net_profit_parent"))
    operating_cash_flow = _safe_float(financial.get("operating_cash_flow"))
    cash_flow_coverage = _provider_cash_flow_coverage(financial)
    financial_flows_comparable = _financial_flows_are_comparable(financial)
    if cash_flow_coverage is None and (
        financial_flows_comparable
        and net_profit is not None
        and net_profit > 0
        and operating_cash_flow is not None
    ):
        cash_flow_coverage = operating_cash_flow / net_profit

    effective_calendar = calendar
    if effective_calendar is None:
        try:
            effective_calendar = ExchangeSessionCalendar()
        except SessionCalendarUnavailable:
            effective_calendar = None
    metrics = _history_metrics(
        history,
        as_of,
        calendar=effective_calendar,
        price_adjustment=price_adjustment,
    )
    volatility = metrics["annualized_volatility_pct"]
    max_drawdown = metrics["max_drawdown_pct"]
    replay_periods = metrics["replay_periods"]
    positive_periods = metrics["positive_replay_periods"]
    roe = _safe_float(growth.get("roe") or financial.get("roe"))
    profit_yoy = _safe_float(growth.get("net_profit_yoy"))
    pe_ratio = _safe_float(valuation.get("pe_ratio"))
    pb_ratio = _safe_float(valuation.get("pb_ratio"))

    essential_missing: List[str] = []
    parsed_price_date = _parse_iso_date(price_date)
    effective_moment = evaluation_moment or datetime.now(SHANGHAI_TZ)
    if effective_moment.tzinfo is None:
        raise ValueError("evaluation_moment must be timezone-aware")
    local_evaluation_date = effective_moment.astimezone(SHANGHAI_TZ).date()
    if normalized_price is None:
        essential_missing.append("当前价格")
    if parsed_price_date is None:
        essential_missing.append("行情日期")
    elif parsed_price_date > as_of:
        essential_missing.append("行情日期晚于评估日")
    elif effective_calendar is not None:
        if as_of == local_evaluation_date:
            latest_expected_session = effective_calendar.completed_session_at(effective_moment)
        else:
            expected_sessions = list(
                effective_calendar.sessions_between(date(as_of.year - 1, 1, 1), as_of)
            )
            latest_expected_session = expected_sessions[-1] if expected_sessions else None
        if latest_expected_session is not None and parsed_price_date < latest_expected_session:
            essential_missing.append("行情日期早于最近应有交易日")
    if ttm_yield is None:
        essential_missing.append("TTM 股息率")
    if ttm_cash is None or ttm_cash <= 0:
        essential_missing.append("TTM 每股现金分红")
    if streak == 0:
        essential_missing.append("现金分红记录")
    if max_drawdown is None or volatility is None:
        essential_missing.append("长期行情")
    if len(replay_periods) < 3:
        essential_missing.append("三年以上完整年度行情")
    period_end = _parse_iso_date(financial.get("period_end") or financial.get("report_date"))
    available_at = _parse_iso_date(financial.get("available_at") or financial.get("announced_at"))
    if period_end is None or period_end > as_of:
        essential_missing.append("财务报告期间")
    if mode == "historical" and (available_at is None or available_at > as_of):
        essential_missing.append("财务披露时点证据")

    if resolved_sector == SectorModel.NORMAL_CORPORATE:
        if net_profit is None or operating_cash_flow is None:
            essential_missing.append("同期间利润/经营现金流")
        elif not financial_flows_comparable and cash_flow_coverage is None:
            essential_missing.append("财务流量期间/口径/单位一致性")
    elif resolved_sector in {
        SectorModel.BANK,
        SectorModel.INSURER,
        SectorModel.BROKER,
        SectorModel.UNSUPPORTED_FINANCIAL,
    }:
        essential_missing.append("金融行业专用监管指标")
    else:
        essential_missing.append("标准行业分类")
    if metrics.get("calendar_status") != "available":
        essential_missing.append("交易日历")

    hard_failures: List[str] = []
    if ttm_yield is not None and ttm_yield > 10:
        hard_failures.append("TTM 股息率超过 10%，需警惕高股息陷阱")
    if resolved_sector == SectorModel.NORMAL_CORPORATE and net_profit is not None and net_profit <= 0:
        hard_failures.append("最新归母净利润非正")
    if resolved_sector == SectorModel.NORMAL_CORPORATE and operating_cash_flow is not None and operating_cash_flow <= 0:
        hard_failures.append("最新经营现金流非正")
    if max_drawdown is not None and max_drawdown > 50:
        hard_failures.append("近年最大回撤超过 50%")
    if volatility is not None and volatility > 50:
        hard_failures.append("年化波动率超过 50%")
    if ttm_yield is not None and ttm_yield <= 0:
        hard_failures.append("近 12 个月没有可验证现金分红")

    sustainability = "偏弱"
    if resolved_sector == SectorModel.NORMAL_CORPORATE and streak >= 4 and cash_flow_coverage is not None and cash_flow_coverage >= 1.0:
        sustainability = "较强"
    elif resolved_sector == SectorModel.NORMAL_CORPORATE and streak >= 3 and cash_flow_coverage is not None and cash_flow_coverage >= 0.8:
        sustainability = "中等"

    risk_tier = "观察"
    if hard_failures:
        risk_tier = "不纳入"
    elif essential_missing:
        risk_tier = "数据不足"
    elif (
        streak >= 4
        and ttm_yield is not None
        and 2.5 <= ttm_yield <= 6.0
        and cash_flow_coverage is not None
        and cash_flow_coverage >= 1.0
        and max_drawdown is not None
        and max_drawdown <= 30
        and volatility is not None
        and volatility <= 30
        and len(replay_periods) >= 4
        and positive_periods >= 3
    ):
        risk_tier = "稳健"
    elif (
        streak >= 3
        and ttm_yield is not None
        and 2.0 <= ttm_yield <= 8.0
        and cash_flow_coverage is not None
        and cash_flow_coverage >= 0.8
        and max_drawdown is not None
        and max_drawdown <= 38
        and volatility is not None
        and volatility <= 38
        and len(replay_periods) >= 3
        and positive_periods >= 2
    ):
        risk_tier = "较稳健"

    score = _yield_score(ttm_yield)
    score += min(streak, 5) * 4
    score += {"较强": 20, "中等": 12, "偏弱": 4}[sustainability]
    if max_drawdown is not None:
        score += 20 if max_drawdown <= 25 else 14 if max_drawdown <= 35 else 6 if max_drawdown <= 45 else 0
    if volatility is not None:
        score += 15 if volatility <= 25 else 10 if volatility <= 35 else 4 if volatility <= 45 else 0
    score += min(positive_periods, 5)

    strengths: List[str] = []
    if ttm_yield is not None:
        strengths.append(f"TTM 税前股息率 {ttm_yield:.2f}%")
    if streak:
        strengths.append(f"可验证连续分红 {streak} 年")
    if resolved_sector == SectorModel.NORMAL_CORPORATE and cash_flow_coverage is not None:
        strengths.append(f"经营现金流/归母净利润 {cash_flow_coverage:.2f} 倍")
    if replay_periods:
        strengths.append(f"最近 {len(replay_periods)} 个完整年度中 {positive_periods} 个历史复权价格阶段为正")

    risks = list(hard_failures)
    if essential_missing:
        risks.append("缺少" + "、".join(essential_missing))
    if profit_yoy is not None and profit_yoy < -10:
        risks.append(f"最新净利润同比 {profit_yoy:.1f}%")
    if streak and streak < 3:
        risks.append("连续分红记录少于 3 年")
    if ttm_yield is not None and 8 < ttm_yield <= 10:
        risks.append("股息率偏高，需核查是否由股价下跌造成")
    if max_drawdown is not None and max_drawdown > 38:
        risks.append(f"近年最大回撤 {max_drawdown:.1f}%")

    qualified = risk_tier in QUALIFIED_TIERS
    ranking_score = min(int(round(score)), 100) if qualified else None
    if resolved_sector in {
        SectorModel.BANK,
        SectorModel.INSURER,
        SectorModel.BROKER,
        SectorModel.UNSUPPORTED_FINANCIAL,
    }:
        failure_code = "unsupported_sector_model"
    elif essential_missing:
        failure_code = "insufficient_evidence"
    else:
        failure_code = "none"

    evidence_issues = [
        _EVIDENCE_ISSUE_CODES.get(reason, "unknown_evidence_issue")
        for reason in essential_missing
    ]
    if failure_code == "unsupported_sector_model":
        terminal_status = SteadyTerminalStatus.UNSUPPORTED_SECTOR_MODEL.value
    elif failure_code == "insufficient_evidence":
        terminal_status = SteadyTerminalStatus.INSUFFICIENT_EVIDENCE.value
    elif qualified:
        terminal_status = SteadyTerminalStatus.EVALUATED_QUALIFIED.value
    else:
        terminal_status = SteadyTerminalStatus.EVALUATED_REJECTED.value

    financial_evidence = (
        dict(financial.get("evidence"))
        if isinstance(financial.get("evidence"), dict)
        else {}
    )
    financial_evidence.update(
        {
            "status": financial_evidence.get("status") or (
                "complete" if available_at is not None else "current_known_only"
            ),
            "evidence_mode": financial_evidence.get("evidence_mode") or (
                "point_in_time" if available_at is not None else "current_known_live"
            ),
            "period_end": period_end.isoformat() if period_end else None,
            "announced_at": _parse_iso_date(financial.get("announced_at")).isoformat()
            if _parse_iso_date(financial.get("announced_at"))
            else None,
            "available_at": available_at.isoformat() if available_at else None,
            "source": financial_evidence.get("source") or "fundamental_context",
            "fetched_at": financial_evidence.get("fetched_at"),
            "unit": financial_evidence.get("unit"),
            "flow_basis": financial_evidence.get("flow_basis"),
            "period_unit_aligned": financial_flows_comparable,
            "provider_ratio_used": cash_flow_coverage is not None and not financial_flows_comparable,
            "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
        }
    )
    dividend_evidence = (
        dict(dividend.get("evidence"))
        if isinstance(dividend.get("evidence"), dict)
        else {}
    )
    dividend_evidence.update(
        {
            "status": dividend_evidence.get("status") or ("complete" if events else "evidence_unavailable"),
            "as_of": as_of.isoformat(),
            "implemented_event_count": len(events),
            "source": dividend_evidence.get("source") or "fundamental_context",
            "fetched_at": dividend_evidence.get("fetched_at"),
            "unit": dividend_evidence.get("unit") or "cash_per_share_pre_tax",
            "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
            "event_date_semantics": "ex_dividend_date",
        }
    )

    return {
        "schema_version": STEADY_INCOME_SCHEMA_VERSION,
        "model_version": STEADY_INCOME_MODEL_VERSION,
        "ruleset_version": STEADY_INCOME_RULESET_VERSION,
        "evaluator_version": STEADY_INCOME_EVALUATOR_VERSION,
        "sector_model_version": STEADY_INCOME_SECTOR_MODEL_VERSION,
        "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
        "price_model_version": STEADY_INCOME_PRICE_MODEL_VERSION,
        "code": code,
        "sector_model": resolved_sector.value,
        "industry": industry or None,
        "risk_tier": risk_tier,
        "public_risk_label": public_risk_label(risk_tier),
        "qualified": qualified,
        "ranking_score": ranking_score,
        "score": ranking_score,
        "score_deprecated": True,
        "current_price": normalized_price,
        "price_date": price_date,
        "ttm_dividend_yield_pct": round(ttm_yield, 4) if ttm_yield is not None else None,
        "ttm_cash_dividend_per_share": round(ttm_cash, 6) if ttm_cash is not None else None,
        "consecutive_dividend_years": streak,
        "dividend_sustainability": sustainability,
        "cash_flow_coverage_ratio": round(cash_flow_coverage, 4) if cash_flow_coverage is not None else None,
        "roe_pct": round(roe, 2) if roe is not None else None,
        "pe_ratio": round(pe_ratio, 2) if pe_ratio is not None else None,
        "pb_ratio": round(pb_ratio, 2) if pb_ratio is not None else None,
        "max_drawdown_pct": max_drawdown,
        "annualized_volatility_pct": volatility,
        "positive_replay_periods": positive_periods,
        "replay_periods": replay_periods,
        "history_coverage": metrics.get("history_coverage", []),
        "price_adjustment": metrics.get("price_adjustment", "unknown"),
        "strengths": strengths[:4],
        "risks": risks[:4],
        "data_status": "完整" if not essential_missing else "部分数据" if strengths else "数据不足",
        "failure_code": failure_code,
        "terminal_status": terminal_status,
        "evidence_issues": evidence_issues,
        "evidence": {
            "financial": financial_evidence,
            "dividend": dividend_evidence,
            "price": {
                "date": parsed_price_date.isoformat() if parsed_price_date else None,
                "adjustment": metrics.get("price_adjustment", "unknown"),
                "provider": context.get("price_provider"),
            },
        },
    }


class SteadyIncomeService:
    """Evaluate current A-share portfolio holdings with a small runtime cache."""

    _cache_lock = threading.RLock()
    _cache: Dict[
        Tuple[str, Optional[int], str, Tuple[Tuple[str, Optional[float], str], ...]],
        Tuple[float, Dict[str, Any]],
    ] = {}

    def __init__(
        self,
        *,
        portfolio_service: Any = None,
        data_manager: Any = None,
        data_manager_factory: Any = None,
        calendar: SessionCalendar | None = None,
    ) -> None:
        self._portfolio_service = portfolio_service
        self._data_manager = data_manager
        self._data_manager_factory = data_manager_factory
        self._manager_local = threading.local()
        self._injected_manager_lock = threading.RLock()
        self._calendar = calendar

    @property
    def portfolio_service(self) -> Any:
        if self._portfolio_service is None:
            from src.services.portfolio_service import PortfolioService

            self._portfolio_service = PortfolioService()
        return self._portfolio_service

    @property
    def data_manager(self) -> Any:
        if self._data_manager is None:
            from data_provider.base import DataFetcherManager

            self._data_manager = DataFetcherManager()
        return self._data_manager

    def _manager_for_worker(self) -> Any:
        if self._data_manager is not None:
            return self._data_manager
        manager = getattr(self._manager_local, "manager", None)
        if manager is None:
            if self._data_manager_factory is not None:
                manager = self._data_manager_factory()
            else:
                from data_provider.base import DataFetcherManager

                manager = DataFetcherManager()
            self._manager_local.manager = manager
        return manager

    @staticmethod
    def _collect_cn_positions(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        positions: Dict[str, Dict[str, Any]] = {}
        for account in snapshot.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            for item in account.get("positions") or []:
                if not isinstance(item, dict):
                    continue
                code = _normalize_a_share_code(item.get("symbol"))
                market = str(item.get("market") or account.get("market") or "").strip().lower()
                if market != "cn" or not _is_a_share_equity_code(code):
                    continue
                price = _safe_float(item.get("last_price"))
                if price is not None and price <= 0:
                    price = None
                current = positions.setdefault(
                    code,
                    {
                        "code": code,
                        "current_price": price,
                        "price_date": item.get("price_date"),
                    },
                )
                candidate_date = str(item.get("price_date") or "")
                if candidate_date > str(current.get("price_date") or ""):
                    current["current_price"] = price
                    current["price_date"] = item.get("price_date")
        return [positions[code] for code in sorted(positions)]

    def _evaluate_position(self, position: Dict[str, Any], as_of: date) -> Dict[str, Any]:
        code = position["code"]
        start_date = (as_of - timedelta(days=365 * 7)).isoformat()
        context: Dict[str, Any] = {}
        history: Any = pd.DataFrame()
        warnings: List[str] = []
        manager = self._manager_for_worker()
        lock = self._injected_manager_lock if self._data_manager is not None else threading.Lock()
        try:
            with lock:
                context = manager.get_fundamental_context(code, budget_seconds=8.0)
        except Exception as exc:
            warnings.append(f"基本面数据不可用：{type(exc).__name__}")
        try:
            with lock:
                history, provider = manager.get_daily_data(
                    code,
                    start_date=start_date,
                    end_date=as_of.isoformat(),
                    days=2000,
                )
            if provider:
                warnings.append(f"历史行情来源：{provider}")
                context["price_provider"] = str(provider)
        except Exception as exc:
            warnings.append(f"历史行情不可用：{type(exc).__name__}")

        result = evaluate_steady_income_candidate(
            code=code,
            current_price=position.get("current_price"),
            price_date=position.get("price_date"),
            context=context,
            history=history,
            as_of=as_of,
            mode="live",
            calendar=self._calendar,
            price_adjustment=str(context.get("price_adjustment") or "unknown"),
        )
        result["data_notes"] = warnings
        return result

    def evaluate_portfolio(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: date | None = None,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of,
            cost_method="fifo",
        )
        as_of_raw = snapshot.get("as_of")
        if not as_of_raw:
            raise ValueError("portfolio snapshot is missing as_of")
        try:
            snapshot_as_of = date.fromisoformat(str(as_of_raw)[:10])
        except ValueError as exc:
            raise ValueError(f"invalid portfolio as_of: {as_of_raw!r}") from exc
        if as_of is not None and snapshot_as_of != as_of:
            raise ValueError(
                f"portfolio snapshot as_of mismatch: requested={as_of.isoformat()} actual={snapshot_as_of.isoformat()}"
            )
        as_of = snapshot_as_of
        positions = self._collect_cn_positions(snapshot)
        warnings: List[str] = []
        position_signature = tuple(
            (
                item["code"],
                item.get("current_price"),
                str(item.get("price_date") or ""),
            )
            for item in positions
        )
        cache_key = (VERSION_FINGERPRINT, account_id, as_of.isoformat(), position_signature)
        now = time.time()
        with self._cache_lock:
            expired_keys = [
                key
                for key, (cached_at, _) in self._cache.items()
                if now - cached_at > CACHE_TTL_SECONDS
            ]
            for key in expired_keys:
                self._cache.pop(key, None)
            if not refresh:
                cached = self._cache.get(cache_key)
                if cached:
                    return copy.deepcopy(cached[1])

        results: List[Dict[str, Any]] = []
        if positions:
            worker_count = min(4, len(positions))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="steady-income") as pool:
                futures = {pool.submit(self._evaluate_position, item, as_of): item["code"] for item in positions}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(
                            {
                                "schema_version": STEADY_INCOME_SCHEMA_VERSION,
                                "model_version": STEADY_INCOME_MODEL_VERSION,
                                "ruleset_version": STEADY_INCOME_RULESET_VERSION,
                                "evaluator_version": STEADY_INCOME_EVALUATOR_VERSION,
                                "sector_model_version": STEADY_INCOME_SECTOR_MODEL_VERSION,
                                "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
                                "price_model_version": STEADY_INCOME_PRICE_MODEL_VERSION,
                                "code": code,
                                "sector_model": SectorModel.UNKNOWN.value,
                                "industry": None,
                                "risk_tier": "数据不足",
                                "public_risk_label": public_risk_label("数据不足"),
                                "qualified": False,
                                "ranking_score": None,
                                "score": None,
                                "score_deprecated": True,
                                "current_price": None,
                                "price_date": None,
                                "ttm_dividend_yield_pct": None,
                                "ttm_cash_dividend_per_share": None,
                                "consecutive_dividend_years": 0,
                                "dividend_sustainability": "偏弱",
                                "cash_flow_coverage_ratio": None,
                                "roe_pct": None,
                                "pe_ratio": None,
                                "pb_ratio": None,
                                "max_drawdown_pct": None,
                                "annualized_volatility_pct": None,
                                "positive_replay_periods": 0,
                                "replay_periods": [],
                                "history_coverage": [],
                                "price_adjustment": "unknown",
                                "strengths": [],
                                "risks": ["评估失败，未纳入稳健收益候选"],
                                "data_status": "数据不足",
                                "failure_code": "unknown_internal",
                                "terminal_status": SteadyTerminalStatus.INTERNAL_ERROR.value,
                                "evidence_issues": ["internal_evaluation_error"],
                                "evidence": {},
                                "data_notes": [f"{type(exc).__name__}"],
                            }
                        )

        results.sort(
            key=lambda item: (
                RISK_TIER_ORDER.get(item["risk_tier"], 99),
                -int(item.get("score") or 0),
                item["code"],
            )
        )
        qualified = [item for item in results if item.get("qualified")]
        excluded = [item for item in results if not item.get("qualified")]
        terminal_distribution = {
            status.value: sum(1 for item in results if item.get("terminal_status") == status.value)
            for status in SteadyTerminalStatus
        }
        count_summary = summarize_deep_evaluation_counts(
            prefilter_count=len(positions),
            requested_count=len(positions),
            terminal_distribution=terminal_distribution,
        )
        response = {
            "schema_version": STEADY_INCOME_SCHEMA_VERSION,
            "model_version": STEADY_INCOME_MODEL_VERSION,
            "ruleset_version": STEADY_INCOME_RULESET_VERSION,
            "evaluator_version": STEADY_INCOME_EVALUATOR_VERSION,
            "sector_model_version": STEADY_INCOME_SECTOR_MODEL_VERSION,
            "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
            "price_model_version": STEADY_INCOME_PRICE_MODEL_VERSION,
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "as_of": as_of.isoformat(),
            "source": "current_portfolio",
            "data_status": (
                "valid_zero"
                if not results
                else "degraded"
                if any(item.get("data_status") != "完整" for item in results)
                else "complete"
            ),
            "selection_mode": "portfolio",
            "universe_count": len(results),
            "prefilter_count": len(results),
            "deep_budget": len(positions),
            **count_summary,
            "is_exhaustive": count_summary["unevaluated_count"] == 0,
            "evaluated_count": count_summary["deep_completed_count"],
            "qualified_count": len(qualified),
            "terminal_status_distribution": terminal_distribution,
            "candidates": qualified,
            "excluded": excluded,
            "warnings": warnings,
            "methodology": {
                "priority": "风险硬门槛优先，规则分仅对证据完整且可比较的低风险候选生成",
                "dividend": "TTM 税前现金分红/当前持仓行情价格",
                "replay": "最近五个交易日历覆盖完整年度之间的历史复权价格回放",
                "limitations": "不预测未来分红；金融行业缺少专用监管证据时直接标为数据不足",
            },
        }
        with self._cache_lock:
            self._cache[cache_key] = (time.time(), copy.deepcopy(response))
        return copy.deepcopy(response)
