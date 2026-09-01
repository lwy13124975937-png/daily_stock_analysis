#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the public, rule-based steady-income dataset for GitHub Pages.

The screen covers the complete active Shanghai/Shenzhen A-share universe in two
stages. A bulk dividend pre-screen is applied to every stock, then a bounded
set of the strongest seeds receives the slower history and cash-flow review.
The module never calls an LLM and never reads position quantities, costs,
market values, or profit/loss fields.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.data.stock_index_loader import find_existing_stock_index_path  # noqa: E402
from src.core.session_calendar import ExchangeSessionCalendar, SessionCalendar  # noqa: E402
from src.reports.contracts import write_json_atomic  # noqa: E402
from src.services.steady_income_service import (  # noqa: E402
    RISK_TIER_ORDER,
    evaluate_steady_income_candidate,
)
from src.services.steady_income_contracts import (  # noqa: E402
    STEADY_INCOME_EVALUATOR_VERSION,
    STEADY_INCOME_EVIDENCE_VERSION,
    STEADY_INCOME_MODEL_VERSION,
    STEADY_INCOME_PRICE_MODEL_VERSION,
    STEADY_INCOME_RULESET_VERSION,
    STEADY_INCOME_SCHEMA_VERSION,
    STEADY_INCOME_SECTOR_MODEL_VERSION,
    HistoricalEvidenceUnavailable,
    SectorModel,
    SteadyTerminalStatus,
    SteadyIncomeProviderUnavailable,
    SteadyIncomeSchemaError,
    public_risk_label,
    resolve_sector_model,
)
from src.services.stock_index_remote_service import (  # noqa: E402
    DEFAULT_STOCK_INDEX_REMOTE_URL,
    validate_stock_index_payload,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_OUTPUT_PATH = ROOT_DIR / "site_data" / "steady_income.json"
MAX_WORKERS = 4
MAX_DEEP_EVALUATIONS = 240
SELECTION_MODE_FIXED_SHORTLIST = "fixed_shortlist"
SELECTION_MODE_EXHAUSTIVE = "exhaustive"
MIN_PLAN_YIELD_PCT = 1.5
MAX_PLAN_YIELD_PCT = 8.0
MIN_LISTING_YEARS = 5.0
MIN_UNIVERSE_SIZE = 3000


def _make_steady_price_manager() -> Any:
    """Build the steady-income price route with the proven history source first.

    The default application route tries several domestic endpoints before
    Yahoo.  For seven-year batch history those endpoints can each consume a
    long timeout while Yahoo already supplies the required adjusted daily
    series.  Domestic providers remain bounded fallbacks.
    """

    from data_provider.akshare_fetcher import AkshareFetcher
    from data_provider.base import DataFetcherManager
    from data_provider.efinance_fetcher import EfinanceFetcher
    from data_provider.yfinance_fetcher import YfinanceFetcher

    yahoo = YfinanceFetcher()
    yahoo.priority = -10
    return DataFetcherManager(fetchers=[yahoo, EfinanceFetcher(), AkshareFetcher()])


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_code(value: Any) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def _is_sh_sz_a_share(code: str, canonical_code: str = "") -> bool:
    """Return whether a code is an active Shanghai/Shenzhen A-share equity."""
    code = _normalize_code(code)
    canonical = str(canonical_code or "").upper()
    if not code or canonical.endswith(".BJ"):
        return False
    if canonical.endswith(".SH"):
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    if canonical.endswith(".SZ"):
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    return code.startswith(("600", "601", "603", "605", "688", "689", "000", "001", "002", "003", "300", "301"))


def _market_label(code: str) -> str:
    return "沪市" if code.startswith("6") else "深市"


def _parse_stock_index(payload: Any) -> list[dict[str, str]]:
    validate_stock_index_payload(payload, min_items=MIN_UNIVERSE_SIZE)
    stocks: dict[str, dict[str, str]] = {}
    for item in payload:
        canonical, display, name, _pinyin, _abbr, _aliases, market, asset_type, active = item[:9]
        code = _normalize_code(display)
        clean_name = str(name or "").strip()
        if market != "CN" or asset_type != "stock" or active is not True:
            continue
        if not _is_sh_sz_a_share(code, str(canonical or "")):
            continue
        if not clean_name or clean_name == code:
            continue
        stocks[code] = {"code": code, "name": clean_name, "market": _market_label(code)}
    result = [stocks[code] for code in sorted(stocks)]
    if len(result) < MIN_UNIVERSE_SIZE:
        raise ValueError(f"Shanghai/Shenzhen stock universe is unexpectedly small: {len(result)}")
    return result


def _read_index_file(path: Path) -> list[Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _call_with_retry(callable_obj: Any, *, attempts: int = 2, label: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return callable_obj()
        except Exception as exc:  # noqa: BLE001 - public data source fallback.
            last_error = exc
            if attempt < attempts:
                time.sleep(float(attempt))
    assert last_error is not None
    raise RuntimeError(f"{label} unavailable: {type(last_error).__name__}: {last_error}") from last_error


def _provider_error_category(exc: Exception) -> tuple[str, bool, bool]:
    text = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, SteadyIncomeSchemaError) or any(
        token in text for token in ("jsondecode", "keyerror", "missing required columns", "schema")
    ):
        return "schema_error", True, False
    if isinstance(exc, (TimeoutError, requests.Timeout)) or "timeout" in text or "timed out" in text:
        return "timeout", False, True
    if isinstance(exc, requests.ConnectionError) or any(
        token in text for token in ("connection", "remote disconnected", "dns", "name resolution")
    ):
        return "network_error", False, True
    if any(token in text for token in ("429", "rate limit", "too many requests")):
        return "rate_limited", False, True
    return "provider_error", False, False


def _provider_call(
    callable_obj: Any,
    *,
    provider: str,
    operation: str,
    evidence_type: str,
    semaphore: threading.BoundedSemaphore,
    validator: Any = None,
    attempts: int = 2,
) -> tuple[Any | None, dict[str, Any]]:
    """Call one public-data operation and return a safe, structured diagnostic."""

    last_error: Exception | None = None
    started = time.monotonic()
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            with semaphore:
                value = callable_obj()
            if validator is not None:
                validator(value)
            return value, {
                "provider": provider,
                "operation": operation,
                "evidence_type": evidence_type,
                "status_category": "ok",
                "exception_class": None,
                "retry_count": attempt - 1,
                "schema_failure": False,
                "network_failure": False,
                "evidence_unavailable": False,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        except Exception as exc:  # noqa: BLE001 - converted to a safe diagnostic below.
            last_error = exc
            if attempt < attempts:
                time.sleep(float(2 ** (attempt - 1)))
    assert last_error is not None
    category, schema_failure, network_failure = _provider_error_category(last_error)
    return None, {
        "provider": provider,
        "operation": operation,
        "evidence_type": evidence_type,
        "status_category": category,
        "exception_class": type(last_error).__name__,
        "retry_count": max(int(attempts), 1) - 1,
        "schema_failure": schema_failure,
        "network_failure": network_failure,
        "evidence_unavailable": True,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _require_table_columns(value: Any, columns: set[str], label: str) -> None:
    if not isinstance(value, pd.DataFrame) or value.empty:
        raise SteadyIncomeSchemaError(f"{label} returned no rows")
    missing = columns.difference(str(column) for column in value.columns)
    if missing:
        raise SteadyIncomeSchemaError(f"{label} missing required columns")


class PublicMarketSource:
    """Load the whole-market index and bulk dividend pre-screen inputs."""

    supports_point_in_time = False

    def __init__(self, *, stock_index_path: Path | None = None) -> None:
        self.stock_index_path = stock_index_path
        # AkShare endpoints use different upstream sites. Keep each upstream
        # bounded independently so a larger worker pool cannot stampede one API.
        self._provider_limits = {
            "sina_finance": threading.BoundedSemaphore(2),
            "sina_dividend": threading.BoundedSemaphore(2),
            "eastmoney_profile": threading.BoundedSemaphore(1),
            "cninfo_profile": threading.BoundedSemaphore(2),
        }
        self._sector_cache: dict[str, tuple[str, str, list[dict[str, Any]]]] = {}
        self._sector_cache_lock = threading.RLock()
        self._metrics_lock = threading.RLock()
        self._logical_provider_calls: Counter[str] = Counter()
        self._cache_metrics: Counter[str] = Counter()

    def _record_provider_call(self, operation: str) -> None:
        with self._metrics_lock:
            self._logical_provider_calls[operation] += 1

    def runtime_metrics(self) -> dict[str, Any]:
        """Return non-sensitive logical call and in-process cache counters."""

        with self._metrics_lock:
            return {
                "logical_provider_calls": dict(sorted(self._logical_provider_calls.items())),
                "logical_provider_call_count": sum(self._logical_provider_calls.values()),
                "cache": dict(sorted(self._cache_metrics.items())),
            }

    def load_sector(self, code: str) -> tuple[str, str, list[dict[str, Any]]]:
        """Resolve a canonical industry with CNInfo primary and EM fallback."""

        with self._sector_cache_lock:
            cached = self._sector_cache.get(code)
        if cached is not None:
            with self._metrics_lock:
                self._cache_metrics["sector_hit"] += 1
            industry, source, diagnostics = cached
            return industry, source, [dict(value) for value in diagnostics]
        with self._metrics_lock:
            self._cache_metrics["sector_miss"] += 1

        import akshare as ak

        diagnostics: list[dict[str, Any]] = []
        industry = ""
        source = ""
        self._record_provider_call("stock_profile_cninfo")
        profile, diagnostic = _provider_call(
            lambda: ak.stock_profile_cninfo(symbol=code),
            provider="akshare-cninfo",
            operation="stock_profile_cninfo",
            evidence_type="sector",
            semaphore=self._provider_limits["cninfo_profile"],
            validator=lambda frame: _require_table_columns(frame, {"所属行业"}, "CNInfo company profile"),
        )
        diagnostics.append(diagnostic)
        if isinstance(profile, pd.DataFrame) and not profile.empty:
            industry = str(profile.iloc[0].get("所属行业") or "").strip()
            if industry:
                source = "akshare.stock_profile_cninfo"
        if not industry:
            self._record_provider_call("stock_individual_info_em")
            profile, diagnostic = _provider_call(
                lambda: ak.stock_individual_info_em(symbol=code),
                provider="akshare-eastmoney",
                operation="stock_individual_info_em",
                evidence_type="sector",
                semaphore=self._provider_limits["eastmoney_profile"],
                validator=lambda frame: _require_table_columns(frame, {"item", "value"}, "security master"),
            )
            diagnostics.append(diagnostic)
            if isinstance(profile, pd.DataFrame) and {"item", "value"}.issubset(profile.columns):
                mapping = {
                    str(row.get("item") or "").strip(): row.get("value")
                    for _, row in profile.iterrows()
                }
                industry = str(mapping.get("行业") or mapping.get("所属行业") or "").strip()
                if industry:
                    source = "akshare.stock_individual_info_em"
        value = (industry, source, diagnostics)
        with self._sector_cache_lock:
            self._sector_cache[code] = value
        return industry, source, [dict(item) for item in diagnostics]

    def load_universe(self) -> tuple[list[dict[str, str]], str]:
        errors: list[str] = []
        try:
            self._record_provider_call("stock_index_remote")
            response = requests.get(DEFAULT_STOCK_INDEX_REMOTE_URL, timeout=20)
            response.raise_for_status()
            return _parse_stock_index(response.json()), DEFAULT_STOCK_INDEX_REMOTE_URL
        except Exception as exc:  # noqa: BLE001 - bundled index remains authoritative fallback.
            errors.append(f"remote index: {type(exc).__name__}")

        path = self.stock_index_path or find_existing_stock_index_path()
        if path is not None:
            try:
                return _parse_stock_index(_read_index_file(path)), f"bundled:{path.name}"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"bundled index: {type(exc).__name__}: {exc}")
        raise RuntimeError("cannot load the Shanghai/Shenzhen market universe; " + "; ".join(errors))

    def load_dividend_plans(self, period: str | int) -> pd.DataFrame:
        import akshare as ak

        period_text = str(period)
        if re.fullmatch(r"\d{4}", period_text):
            period_text += "1231"
        if not re.fullmatch(r"\d{8}", period_text):
            raise ValueError(f"invalid dividend report period: {period!r}")
        self._record_provider_call("stock_fhps_em")
        return _call_with_retry(
            lambda: ak.stock_fhps_em(date=period_text),
            attempts=2,
            label=f"{period_text} dividend plans",
        )

    def load_dividend_plan_periods(self, as_of: date) -> tuple[pd.DataFrame, list[str]]:
        periods = []
        for year in range(as_of.year - 2, as_of.year + 1):
            for suffix in ("0630", "1231"):
                period = f"{year}{suffix}"
                if _date_from_any(period) and _date_from_any(period) <= as_of:
                    periods.append(period)
        frames: list[pd.DataFrame] = []
        notes: list[str] = []
        errors: list[str] = []
        for period in periods[-4:]:
            try:
                frame = self.load_dividend_plans(period)
                if not isinstance(frame, pd.DataFrame):
                    raise SteadyIncomeSchemaError(f"{period} dividend plans is not a table")
                frame = frame.copy()
                frame["证据报告期"] = period
                frames.append(frame)
                notes.append(period)
            except Exception as exc:
                errors.append(f"{period}:{type(exc).__name__}")
        if not frames:
            raise SteadyIncomeProviderUnavailable(
                "all interim/annual dividend plan periods are unavailable: " + ", ".join(errors)
            )
        return pd.concat(frames, ignore_index=True, sort=False), notes

    def load_dividend_history(self) -> pd.DataFrame:
        import akshare as ak

        self._record_provider_call("stock_history_dividend")
        return _call_with_retry(
            ak.stock_history_dividend,
            attempts=2,
            label="historical dividend summary",
        )

    def load_deep_context(self, code: str, as_of: date) -> tuple[dict[str, Any], list[str]]:
        """Load only the financial and dividend evidence required by the risk gate."""
        import akshare as ak

        notes: list[str] = []
        financial = pd.DataFrame()
        dividends = pd.DataFrame()
        industry = ""
        industry_source = ""
        diagnostics: list[dict[str, Any]] = []
        self._record_provider_call("stock_financial_abstract")
        value, diagnostic = _provider_call(
            lambda: ak.stock_financial_abstract(symbol=code),
            provider="akshare-sina",
            operation="stock_financial_abstract",
            evidence_type="financial",
            semaphore=self._provider_limits["sina_finance"],
            validator=lambda frame: _require_table_columns(frame, {"指标"}, "financial abstract"),
        )
        diagnostics.append(diagnostic)
        if isinstance(value, pd.DataFrame):
            financial = value
            notes.append("财务证据来源：AkShare 财务摘要")
        else:
            notes.append("财务证据不可用")

        self._record_provider_call("stock_history_dividend_detail")
        value, diagnostic = _provider_call(
            lambda: ak.stock_history_dividend_detail(symbol=code, indicator="分红", date=""),
            provider="akshare-sina",
            operation="stock_history_dividend_detail",
            evidence_type="dividend",
            semaphore=self._provider_limits["sina_dividend"],
            validator=lambda frame: _require_table_columns(
                frame, {"派息", "进度", "除权除息日"}, "dividend history"
            ),
        )
        diagnostics.append(diagnostic)
        if isinstance(value, pd.DataFrame):
            dividends = value
            notes.append("分红证据来源：AkShare 历史分红")
        else:
            notes.append("分红证据不可用")

        industry, industry_source, sector_diagnostics = self.load_sector(code)
        diagnostics.extend(sector_diagnostics)
        notes.append(f"行业分类来源：{industry_source}" if industry else "行业分类证据不可用")
        context = _build_deep_context(
            financial,
            dividends,
            as_of=as_of,
            industry=industry,
            industry_source=industry_source,
        )
        context["_provider_diagnostics"] = diagnostics
        return context, notes


def _date_from_any(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        if isinstance(value, (int, float)) and abs(float(value)) > 10_000_000_000:
            stamp = pd.to_datetime(value, unit="ms", errors="coerce")
        else:
            stamp = pd.to_datetime(value, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.date()


def _indicator_value(frame: pd.DataFrame, indicator_names: tuple[str, ...], column: str) -> float | None:
    if "指标" not in frame.columns or column not in frame.columns:
        return None
    names = frame["指标"].astype(str).str.strip()
    for indicator in indicator_names:
        matches = frame.loc[names == indicator, column]
        for value in matches:
            number = _safe_float(value)
            if number is not None:
                return number
    return None


def _financial_evidence(
    frame: Any,
    *,
    as_of: date,
    mode: str = "live",
    availability_by_period: dict[str, Any] | None = None,
    unit: str | None = None,
    flow_basis: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize financial evidence without treating period end as publication time."""
    if not isinstance(frame, pd.DataFrame) or frame.empty or "指标" not in frame.columns:
        return {}, {}
    report_columns: list[tuple[date, str]] = []
    for column in frame.columns:
        text = str(column).strip()
        if not re.fullmatch(r"\d{8}", text):
            continue
        report_date = _date_from_any(text)
        if report_date is not None and report_date <= as_of:
            report_columns.append((report_date, str(column)))
    report_columns.sort(reverse=True)

    for report_date, column in report_columns:
        announced_at = _date_from_any((availability_by_period or {}).get(column))
        if mode == "historical" and (announced_at is None or announced_at > as_of):
            continue
        net_profit = _indicator_value(frame, ("归母净利润", "归属母公司股东净利润"), column)
        operating_cash_flow = _indicator_value(
            frame,
            ("经营现金流量净额", "经营活动产生的现金流量净额"),
            column,
        )
        provider_cash_flow_ratio = _indicator_value(
            frame,
            (
                "经营活动净现金/归属母公司的净利润",
                "经营活动净现金流/归属母公司的净利润",
                "经营现金流量净额/归母净利润",
            ),
            column,
        )
        if net_profit is None or operating_cash_flow is None:
            continue
        roe = _indicator_value(frame, ("净资产收益率(ROE)", "净资产收益率_ROE"), column)
        return (
            {"roe": roe} if roe is not None else {},
            {
                "period_end": report_date.isoformat(),
                "announced_at": announced_at.isoformat() if announced_at else None,
                "available_at": announced_at.isoformat() if announced_at else None,
                "net_profit_parent": net_profit,
                "operating_cash_flow": operating_cash_flow,
                "net_profit_period_end": report_date.isoformat(),
                "operating_cash_flow_period_end": report_date.isoformat(),
                "net_profit_unit": unit,
                "operating_cash_flow_unit": unit,
                "net_profit_flow_basis": flow_basis,
                "operating_cash_flow_flow_basis": flow_basis,
                "cash_flow_coverage_ratio": provider_cash_flow_ratio,
                "cash_flow_coverage_period_end": report_date.isoformat(),
                "cash_flow_coverage_source": (
                    "provider_reported_same_period_ratio"
                    if provider_cash_flow_ratio is not None
                    else None
                ),
                "roe": roe,
                "evidence": {
                    "status": "point_in_time" if announced_at else "current_known_only",
                    "evidence_mode": "point_in_time" if announced_at else "current_known_live",
                    "source": "akshare.stock_financial_abstract",
                    "fetched_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
                    "unit": unit,
                    "flow_basis": flow_basis,
                    "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
                    "period_end": report_date.isoformat(),
                    "announced_at": announced_at.isoformat() if announced_at else None,
                    "available_at": announced_at.isoformat() if announced_at else None,
                },
            },
        )
    return {}, {
        "evidence": {
            "status": "evidence_unavailable",
            "evidence_mode": "point_in_time" if mode == "historical" else "current_known_live",
            "reason": (
                "financial_disclosure_date_missing_or_future"
                if mode == "historical"
                else "financial_values_missing"
            ),
            "source": "akshare.stock_financial_abstract",
            "fetched_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
            "price_model_version": STEADY_INCOME_PRICE_MODEL_VERSION,
        }
    }


def _dividend_evidence(frame: Any, *, as_of: date) -> dict[str, Any]:
    """Normalize implemented cash dividends; AkShare's `派息` is per ten shares."""
    if not isinstance(frame, pd.DataFrame) or frame.empty or "派息" not in frame.columns:
        return {}
    events_by_key: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        progress = str(row.get("进度") or "").strip()
        if "实施" not in progress:
            continue
        event_date = _date_from_any(row.get("除权除息日"))
        per_ten = _safe_float(row.get("派息"))
        if event_date is None or event_date > as_of or per_ten is None or per_ten <= 0:
            continue
        per_share = per_ten / 10.0
        plan_id = str(row.get("方案ID") or row.get("方案编号") or "").strip()
        # The implementation date is the cash-event identity. Provider rows may
        # gain/lose a plan id across proposal updates, so plan id alone cannot
        # prevent the same implemented event from being counted twice.
        key = event_date.isoformat()
        candidate = {
            "plan_id": plan_id or None,
            "event_date": event_date.isoformat(),
            "ex_dividend_date": event_date.isoformat(),
            "cash_dividend_per_share": round(per_share, 6),
            "is_pre_tax": True,
            "implemented": True,
            "implementation_status": "implemented",
            "announcement_date": (
                _date_from_any(row.get("公告日期")).isoformat()
                if _date_from_any(row.get("公告日期"))
                else None
            ),
        }
        previous = events_by_key.get(key)
        if previous is None or str(candidate.get("announcement_date") or "") >= str(
            previous.get("announcement_date") or ""
        ):
            events_by_key[key] = candidate
    events = sorted(events_by_key.values(), key=lambda item: item["event_date"], reverse=True)
    if not events:
        return {}
    ttm_start = as_of - timedelta(days=365)
    ttm_events = [
        item
        for item in events
        if ttm_start <= date.fromisoformat(item["event_date"]) <= as_of
    ]
    return {
        "events": events,
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item["cash_dividend_per_share"]) for item in ttm_events), 6)
            if ttm_events
            else None
        ),
        "coverage": "implemented_cash_dividend_pre_tax",
        "as_of": as_of.isoformat(),
        "evidence": {
            "status": "complete",
            "source": "akshare.stock_history_dividend_detail",
            "fetched_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "unit": "cash_per_share_pre_tax",
            "evidence_version": STEADY_INCOME_EVIDENCE_VERSION,
            "event_date_semantics": "ex_dividend_date",
        },
    }


def _build_deep_context(
    financial: Any,
    dividends: Any,
    *,
    as_of: date,
    industry: str = "",
    industry_source: str = "",
    mode: str = "live",
    availability_by_period: dict[str, Any] | None = None,
    financial_unit: str | None = None,
    flow_basis: str | None = None,
) -> dict[str, Any]:
    growth, financial_report = _financial_evidence(
        financial,
        as_of=as_of,
        mode=mode,
        availability_by_period=availability_by_period,
        unit=financial_unit,
        flow_basis=flow_basis,
    )
    dividend = _dividend_evidence(dividends, as_of=as_of)
    earnings: dict[str, Any] = {}
    if financial_report:
        earnings["financial_report"] = financial_report
    if dividend:
        earnings["dividend"] = dividend
    return {
        "security_master": {
            "industry": industry or None,
            "source": industry_source or ("provider_security_master" if industry else None),
            "as_of": as_of.isoformat(),
        },
        "growth": {"data": growth},
        "earnings": {"data": earnings},
    }


def _yield_quality_score(yield_pct: float) -> float:
    if 3.0 <= yield_pct <= 6.0:
        return 30.0
    distance = min(abs(yield_pct - 3.0), abs(yield_pct - 6.0))
    return max(5.0, 30.0 - distance * 8.0)


def _payout_quality_score(payout_ratio: float) -> float:
    return max(0.0, 20.0 - abs(payout_ratio - 0.5) * 45.0)


def _select_sector_stratified(
    eligible: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or not eligible:
        return []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in eligible:
        groups[str(item.get("industry") or "industry_unknown")].append(item)
    for items in groups.values():
        items.sort(key=lambda item: (-float(item["seed_score"]), item["code"]))
    ordered_groups = sorted(groups, key=lambda key: (-len(groups[key]), key))
    selected: list[dict[str, Any]] = []
    cursor = 0
    while len(selected) < limit:
        added = False
        for group in ordered_groups:
            items = groups[group]
            if cursor < len(items):
                selected.append(items[cursor])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        cursor += 1
    return selected


def _selection_queue_sensitivity(
    eligible: list[dict[str, Any]],
    *,
    budgets: tuple[int, ...] = (30, 60, 120),
) -> dict[str, Any]:
    """Compare cheap-screen deep queues without extra provider requests."""

    snapshots: dict[int, list[dict[str, Any]]] = {
        budget: _select_sector_stratified(eligible, limit=budget)
        for budget in sorted({max(int(value), 0) for value in budgets})
    }
    baseline_budget = min(snapshots) if snapshots else 0
    baseline_codes = [item["code"] for item in snapshots.get(baseline_budget, [])]
    baseline_set = set(baseline_codes)
    rows: list[dict[str, Any]] = []
    for budget, selected in snapshots.items():
        codes = [item["code"] for item in selected]
        common = baseline_set.intersection(codes)
        positions = {code: index for index, code in enumerate(codes)}
        displacement = (
            sum(abs(index - positions[code]) for index, code in enumerate(baseline_codes) if code in positions)
            / len(common)
            if common
            else None
        )
        rows.append(
            {
                "deep_budget": budget,
                "selected_count": len(codes),
                "overlap_with_30_count": len(common),
                "overlap_with_30_ratio": round(len(common) / len(baseline_set), 4) if baseline_set else None,
                "mean_rank_displacement_from_30": round(displacement, 4) if displacement is not None else None,
                "sector_coverage_count": len({str(item.get("industry") or "industry_unknown") for item in selected}),
                "deep_failure_rate": None,
                "deep_failure_rate_status": "selection_only_not_evaluated",
            }
        )
    return {
        "audit_scope": "cheap_screen_selection_queue_only",
        "baseline_budget": baseline_budget,
        "budgets": rows,
    }


def _prefilter_market(
    universe: list[dict[str, str]],
    plans: pd.DataFrame,
    dividend_history: pd.DataFrame,
    *,
    as_of: date,
    max_deep_evaluations: int = MAX_DEEP_EVALUATIONS,
    include_eligible_queue: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required_plan_columns = {
        "代码",
        "名称",
        "现金分红-现金分红比例",
        "现金分红-股息率",
        "每股收益",
        "最新公告日期",
        "方案进度",
    }
    required_history_columns = {"代码", "上市日期", "分红次数"}
    if not isinstance(plans, pd.DataFrame) or not required_plan_columns.issubset(plans.columns):
        raise SteadyIncomeSchemaError("dividend plan table is missing required columns")
    if (
        not isinstance(dividend_history, pd.DataFrame)
        or not required_history_columns.issubset(dividend_history.columns)
    ):
        raise SteadyIncomeSchemaError("historical dividend summary is missing required columns")

    universe_by_code = {item["code"]: item for item in universe}
    rejected = Counter()
    history_by_code: dict[str, dict[str, Any]] = {}
    for _, row in dividend_history.iterrows():
        code = _normalize_code(row.get("代码"))
        if code in universe_by_code:
            history_by_code[code] = {
                "dividend_count": int(_safe_float(row.get("分红次数")) or 0),
                "listing_date": _date_from_any(row.get("上市日期")),
            }

    latest_rows: dict[str, pd.Series] = {}
    for _, row in plans.iterrows():
        code = _normalize_code(row.get("代码"))
        if code not in universe_by_code:
            continue
        announcement_date = _date_from_any(row.get("最新公告日期"))
        if announcement_date is None or announcement_date > as_of:
            rejected["announcement_not_known_as_of"] += 1
            continue
        existing = latest_rows.get(code)
        current_date = announcement_date
        existing_date = _date_from_any(existing.get("最新公告日期")) if existing is not None else None
        if existing is None or current_date >= (existing_date or date.min):
            latest_rows[code] = row

    eligible: list[dict[str, Any]] = []
    for code, row in latest_rows.items():
        stock = universe_by_code[code]
        name = str(stock.get("name") or row.get("名称") or code).strip()
        if re.match(r"^(?:\*?ST|退)", name, flags=re.IGNORECASE):
            rejected["current_security_status_excluded"] += 1
            continue
        plan_yield = _safe_float(row.get("现金分红-股息率"))
        plan_dps_per_ten = _safe_float(row.get("现金分红-现金分红比例"))
        eps = _safe_float(row.get("每股收益"))
        if plan_yield is None or plan_dps_per_ten is None or eps is None:
            rejected["prefilter_fields_missing"] += 1
            continue
        yield_pct = plan_yield * 100.0
        dps = plan_dps_per_ten / 10.0
        payout_ratio = dps / eps if eps > 0 else None
        history = history_by_code.get(code, {})
        dividend_count = int(history.get("dividend_count") or 0)
        listing_date = history.get("listing_date")
        listing_years = ((as_of - listing_date).days / 365.25) if isinstance(listing_date, date) else 0.0
        if not (MIN_PLAN_YIELD_PCT <= yield_pct <= MAX_PLAN_YIELD_PCT):
            rejected["plan_yield_outside_prefilter"] += 1
            continue
        if not (dps > 0 and eps > 0 and payout_ratio is not None and 0.10 <= payout_ratio <= 0.90):
            rejected["payout_or_profit_prefilter_failed"] += 1
            continue
        if listing_years < MIN_LISTING_YEARS:
            rejected["listing_history_too_short"] += 1
            continue

        progress = str(row.get("方案进度") or "").strip()
        industry = str(row.get("所属行业") or stock.get("industry") or "").strip()
        continuity_score = min(dividend_count, 20) / 20.0 * 35.0
        listing_score = min(listing_years, 20.0) / 20.0 * 10.0
        implementation_score = 5.0 if "实施" in progress else 0.0
        seed_score = (
            continuity_score
            + _yield_quality_score(yield_pct)
            + _payout_quality_score(payout_ratio)
            + listing_score
            + implementation_score
        )
        eligible.append(
            {
                "code": code,
                "name": name,
                "market": stock["market"],
                "industry": industry or None,
                "sector_model_hint": resolve_sector_model(industry).value,
                "seed_score": round(seed_score, 3),
                "plan_yield_pct": round(yield_pct, 4),
                "plan_dps": round(dps, 6),
                "payout_ratio": round(payout_ratio, 4),
                "dividend_count": dividend_count,
                "listing_years": round(listing_years, 2),
                "plan_status": progress,
            }
        )

    eligible.sort(key=lambda item: (-float(item["seed_score"]), -float(item["plan_yield_pct"]), item["code"]))
    for position, item in enumerate(eligible, start=1):
        item["prefilter_position"] = position
    selected = _select_sector_stratified(eligible, limit=max(int(max_deep_evaluations), 0))
    sector_distribution = Counter(str(item.get("industry") or "industry_unknown") for item in eligible)
    selected_sector_distribution = Counter(str(item.get("industry") or "industry_unknown") for item in selected)
    stats: dict[str, Any] = {
        "universe_count": len(universe),
        "known_plan_count": len(latest_rows),
        "prefilter_eligible_count": len(eligible),
        "deep_selected_count": len(selected),
        "rejected_by_reason": dict(sorted(rejected.items())),
        "prefilter_sector_distribution": dict(sorted(sector_distribution.items())),
        "deep_selection_sector_coverage": dict(sorted(selected_sector_distribution.items())),
        "selection_sensitivity": _selection_queue_sensitivity(eligible),
    }
    if include_eligible_queue:
        stats["_eligible_queue"] = eligible
    return selected, stats


def _latest_quote(frame: Any, as_of: date) -> tuple[float | None, str | None]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None, None
    if "date" not in frame.columns or "close" not in frame.columns:
        return None, None
    work = frame[["date", "close"]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["date", "close"])
    work = work.loc[(work["close"] > 0) & (work["date"].dt.date <= as_of)]
    if work.empty:
        return None, None
    latest = work.sort_values("date").iloc[-1]
    return float(latest["close"]), latest["date"].date().isoformat()


def _has_evidence_failure(item: dict[str, Any]) -> bool:
    """Count evidence failures independently from the final risk tier.

    A stock may satisfy a hard exclusion rule and still lack mandatory evidence.
    The exclusion label must not hide that data-quality failure from the funnel.
    """

    return str(item.get("failure_code") or "none") != "none"


_TERMINAL_STATUSES = tuple(status.value for status in SteadyTerminalStatus)
_ISSUE_EVIDENCE_TYPE = {
    "missing_current_price": "price",
    "missing_price_date": "price",
    "future_price_date": "price",
    "stale_price_date": "price",
    "missing_ttm_dividend_yield": "dividend",
    "missing_ttm_cash_dividend": "dividend",
    "missing_implemented_dividend_history": "dividend",
    "missing_price_history": "history",
    "insufficient_history_coverage": "history",
    "missing_financial_period": "financial",
    "missing_available_at": "financial",
    "missing_financial_flows": "financial",
    "unverifiable_financial_flow_semantics": "financial",
    "missing_regulatory_metrics": "sector",
    "missing_canonical_sector": "sector",
    "trading_calendar_unavailable": "history",
}


def _evidence_statuses(item: dict[str, Any]) -> dict[str, str]:
    issues = set(str(value) for value in item.get("evidence_issues") or [])
    failed_types = {_ISSUE_EVIDENCE_TYPE.get(value, "unknown") for value in issues}
    return {
        evidence_type: "unavailable" if evidence_type in failed_types else "complete"
        for evidence_type in ("price", "dividend", "financial", "sector", "history")
    }


def _failed_diagnostics_by_evidence(
    diagnostics: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for diagnostic in diagnostics:
        if diagnostic.get("status_category") != "ok":
            evidence_type = str(diagnostic.get("evidence_type") or "unknown")
            targets = {
                "price_history": ("price", "history"),
                "deep_context": ("financial", "dividend", "sector"),
            }.get(evidence_type, (evidence_type,))
            for target in targets:
                by_evidence[target].append(diagnostic)
    return by_evidence


def _unresolved_provider_diagnostics(
    item: dict[str, Any], diagnostics: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    missing_types = {
        _ISSUE_EVIDENCE_TYPE.get(str(issue), "unknown")
        for issue in item.get("evidence_issues") or []
    }
    failed = _failed_diagnostics_by_evidence(diagnostics)
    return [
        diagnostic
        for evidence_type in missing_types
        for diagnostic in failed.get(evidence_type, [])
    ]


def _finalize_terminal_status(
    item: dict[str, Any], diagnostics: list[dict[str, Any]]
) -> dict[str, Any]:
    unresolved = _unresolved_provider_diagnostics(item, diagnostics)
    if item.get("sector_model") in {
        SectorModel.BANK.value,
        SectorModel.INSURER.value,
        SectorModel.BROKER.value,
        SectorModel.UNSUPPORTED_FINANCIAL.value,
    }:
        terminal = SteadyTerminalStatus.UNSUPPORTED_SECTOR_MODEL.value
    elif str(item.get("failure_code") or "none") == "none":
        terminal = (
            SteadyTerminalStatus.EVALUATED_QUALIFIED.value
            if item.get("qualified")
            else SteadyTerminalStatus.EVALUATED_REJECTED.value
        )
    elif unresolved:
        terminal = SteadyTerminalStatus.PROVIDER_FAILURE.value
        item["failure_code"] = "provider_unavailable"
    else:
        terminal = SteadyTerminalStatus.INSUFFICIENT_EVIDENCE.value
    item["terminal_status"] = terminal
    item["evidence_status"] = _evidence_statuses(item)
    item["provider_diagnostics"] = diagnostics
    item["provider_failures"] = [
        f"{value.get('provider')}:{value.get('operation')}:{value.get('status_category')}"
        for value in unresolved
    ]
    return item


class SteadyIncomeDatasetBuilder:
    def __init__(
        self,
        data_manager: Any = None,
        *,
        data_manager_factory: Any = None,
        market_source: Any = None,
        max_workers: int = MAX_WORKERS,
        max_deep_evaluations: int = MAX_DEEP_EVALUATIONS,
        calendar: SessionCalendar | None = None,
        mode: str = "live",
    ) -> None:
        self._data_manager = data_manager
        self._data_manager_factory = data_manager_factory
        self._manager_local = threading.local()
        self._injected_manager_lock = threading.RLock()
        self.market_source = market_source or PublicMarketSource()
        self.max_workers = max(1, int(max_workers))
        self.max_deep_evaluations = max(0, int(max_deep_evaluations))
        self.calendar = calendar or ExchangeSessionCalendar()
        if mode not in {"live", "historical"}:
            raise ValueError("mode must be 'live' or 'historical'")
        self.mode = mode

    @property
    def data_manager(self) -> Any:
        if self._data_manager is None:
            self._data_manager = _make_steady_price_manager()
        return self._data_manager

    def _manager_for_worker(self) -> Any:
        if self._data_manager is not None:
            return self._data_manager
        manager = getattr(self._manager_local, "manager", None)
        if manager is None:
            if self._data_manager_factory is not None:
                manager = self._data_manager_factory()
            else:
                manager = _make_steady_price_manager()
            self._manager_local.manager = manager
        return manager

    def _select_supported_seeds(
        self, eligible: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fill the deep budget with normal corporates, not known financials."""

        selected: list[dict[str, Any]] = []
        unsupported = Counter()
        sector_failures = Counter()
        if self.max_deep_evaluations <= 0:
            return selected, {
                "predeep_unsupported_sector_model": {},
                "predeep_unsupported_count": 0,
                "predeep_sector_provider_failures": {},
                "normal_corporate_deep_selected_count": 0,
            }
        queue = _select_sector_stratified(eligible, limit=len(eligible))
        for raw_seed in queue:
            seed = dict(raw_seed)
            industry = str(seed.get("industry") or "").strip()
            model = resolve_sector_model(industry)
            if model == SectorModel.UNKNOWN and hasattr(self.market_source, "load_sector"):
                industry, source, diagnostics = self.market_source.load_sector(seed["code"])
                seed["industry"] = industry or None
                seed["industry_source"] = source or None
                model = resolve_sector_model(industry)
                if model == SectorModel.UNKNOWN:
                    for diagnostic in diagnostics:
                        if diagnostic.get("status_category") != "ok":
                            sector_failures[str(diagnostic.get("operation") or "unknown")] += 1
            seed["sector_model_hint"] = model.value
            if model in {
                SectorModel.BANK,
                SectorModel.INSURER,
                SectorModel.BROKER,
                SectorModel.UNSUPPORTED_FINANCIAL,
            }:
                unsupported[model.value] += 1
                continue
            if model == SectorModel.UNKNOWN:
                continue
            selected.append(seed)
            if len(selected) >= self.max_deep_evaluations:
                break
        return selected, {
            "predeep_unsupported_sector_model": dict(sorted(unsupported.items())),
            "predeep_unsupported_count": sum(unsupported.values()),
            "predeep_sector_provider_failures": dict(sorted(sector_failures.items())),
            "normal_corporate_deep_selected_count": len(selected),
        }

    def _evaluate(
        self,
        seed: dict[str, Any],
        as_of: date,
        evaluation_moment: datetime | None = None,
    ) -> dict[str, Any]:
        code = seed["code"]
        context: dict[str, Any] = {}
        history: Any = pd.DataFrame()
        notes: list[str] = []
        diagnostics: list[dict[str, Any]] = []

        try:
            loader = getattr(self.market_source, "load_deep_context")
            context, evidence_notes = loader(code, as_of)
            notes.extend(str(note) for note in evidence_notes if note)
            source_diagnostics = context.pop("_provider_diagnostics", [])
            if isinstance(source_diagnostics, list):
                diagnostics.extend(
                    dict(value) for value in source_diagnostics if isinstance(value, dict)
                )
        except Exception as exc:
            category, schema_failure, network_failure = _provider_error_category(exc)
            diagnostics.append(
                {
                    "provider": type(self.market_source).__name__,
                    "operation": "load_deep_context",
                    "evidence_type": "deep_context",
                    "status_category": category,
                    "exception_class": type(exc).__name__,
                    "retry_count": 0,
                    "schema_failure": schema_failure,
                    "network_failure": network_failure,
                    "evidence_unavailable": True,
                }
            )
            notes.append("稳健收益深度证据不可用")

        manager = self._manager_for_worker()
        lock = self._injected_manager_lock if self._data_manager is not None else threading.Lock()
        try:
            with lock:
                history, provider = manager.get_daily_data(
                    code,
                    start_date=(as_of - timedelta(days=365 * 7)).isoformat(),
                    end_date=as_of.isoformat(),
                    days=2000,
                )
            if provider:
                notes.append(f"历史行情来源：{provider}")
                context["price_provider"] = str(provider)
            diagnostics.append(
                {
                    "provider": str(provider or type(manager).__name__),
                    "operation": "get_daily_data",
                    "evidence_type": "price_history",
                    "status_category": "ok",
                    "exception_class": None,
                    "retry_count": 0,
                    "schema_failure": False,
                    "network_failure": False,
                    "evidence_unavailable": False,
                }
            )
        except Exception as exc:
            category, schema_failure, network_failure = _provider_error_category(exc)
            diagnostics.append(
                {
                    "provider": type(manager).__name__,
                    "operation": "get_daily_data",
                    "evidence_type": "price_history",
                    "status_category": category,
                    "exception_class": type(exc).__name__,
                    "retry_count": 0,
                    "schema_failure": schema_failure,
                    "network_failure": network_failure,
                    "evidence_unavailable": True,
                }
            )
            notes.append("历史行情证据不可用")

        current_price, price_date = _latest_quote(history, as_of)
        master = context.setdefault("security_master", {})
        if isinstance(master, dict) and not master.get("industry") and seed.get("industry"):
            master["industry"] = seed["industry"]
            master["source"] = "dividend_plan_provider"
        result = evaluate_steady_income_candidate(
            code=code,
            current_price=current_price,
            price_date=price_date,
            context=context,
            history=history,
            as_of=as_of,
            mode=self.mode,
            calendar=self.calendar,
            price_adjustment=str(context.get("price_adjustment") or "provider_unspecified_adjustment"),
            evaluation_moment=evaluation_moment,
        )
        result.update(
            {
                "name": seed["name"],
                "market": seed["market"],
                "preselection": {
                    key: seed.get(key)
                    for key in (
                        "deep_queue_position",
                        "prefilter_position",
                        "seed_score",
                        "plan_yield_pct",
                        "payout_ratio",
                        "dividend_count",
                        "listing_years",
                        "plan_status",
                    )
                },
                "data_notes": notes,
            }
        )
        return _finalize_terminal_status(result, diagnostics)

    def build(self, *, as_of: date | None = None) -> dict[str, Any]:
        evaluation_moment = datetime.now(SHANGHAI_TZ)
        evaluation_date = as_of or evaluation_moment.date()
        if self.mode == "historical" and not bool(getattr(self.market_source, "supports_point_in_time", False)):
            raise HistoricalEvidenceUnavailable(
                "historical mode requires point-in-time universe, security status, disclosure dates, and dividend events"
            )
        universe, universe_source = self.market_source.load_universe()
        if hasattr(self.market_source, "load_dividend_plan_periods"):
            plans, plan_periods = self.market_source.load_dividend_plan_periods(evaluation_date)
        else:
            plans = self.market_source.load_dividend_plans(evaluation_date.year - 1)
            plan_periods = [f"{evaluation_date.year - 1}1231"]
        dividend_history = self.market_source.load_dividend_history()
        seeds, stats = _prefilter_market(
            universe,
            plans,
            dividend_history,
            as_of=evaluation_date,
            max_deep_evaluations=self.max_deep_evaluations,
            include_eligible_queue=True,
        )
        eligible_queue = stats.pop("_eligible_queue", [])
        if eligible_queue:
            seeds, selection_stats = self._select_supported_seeds(eligible_queue)
            stats.update(selection_stats)
            stats["deep_selected_count"] = len(seeds)
            stats["deep_selection_sector_coverage"] = dict(
                sorted(Counter(str(item.get("industry") or "industry_unknown") for item in seeds).items())
            )
        results: list[dict[str, Any]] = []
        for position, seed in enumerate(seeds, start=1):
            seed["deep_queue_position"] = position

        if seeds:
            worker_count = min(self.max_workers, len(seeds))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="steady-market-pages") as pool:
                futures = {
                    pool.submit(self._evaluate, seed, evaluation_date, evaluation_moment): seed
                    for seed in seeds
                }
                for future in as_completed(futures):
                    seed = futures[future]
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
                                "code": seed["code"],
                                "name": seed["name"],
                                "market": seed["market"],
                                "risk_tier": "数据不足",
                                "public_risk_label": public_risk_label("数据不足"),
                                "qualified": False,
                                "ranking_score": None,
                                "score": None,
                                "current_price": None,
                                "price_date": None,
                                "ttm_dividend_yield_pct": None,
                                "consecutive_dividend_years": 0,
                                "dividend_sustainability": "偏弱",
                                "max_drawdown_pct": None,
                                "annualized_volatility_pct": None,
                                "positive_replay_periods": 0,
                                "replay_periods": [],
                                "history_coverage": [],
                                "price_adjustment": "unknown",
                                "strengths": [],
                                "risks": ["公开数据不足，未纳入低风险候选"],
                                "data_status": "数据不足",
                                "preselection": {
                                    "deep_queue_position": seed["deep_queue_position"],
                                    "prefilter_position": seed.get("prefilter_position"),
                                    "seed_score": seed["seed_score"],
                                },
                                "data_notes": [f"评估不可用：{type(exc).__name__}"],
                                "provider_failures": [],
                                "provider_diagnostics": [],
                                "failure_code": "unknown_internal",
                                "terminal_status": SteadyTerminalStatus.INTERNAL_ERROR.value,
                                "evidence_issues": ["internal_evaluation_error"],
                                "evidence_status": {
                                    key: "unknown"
                                    for key in ("price", "dividend", "financial", "sector", "history")
                                },
                                "sector_model": seed.get("sector_model_hint") or SectorModel.UNKNOWN.value,
                                "evidence": {},
                            }
                        )

        results.sort(
            key=lambda item: (
                RISK_TIER_ORDER.get(str(item.get("risk_tier")), 99),
                -int(item.get("ranking_score") or 0),
                str(item.get("code") or ""),
            )
        )
        candidates = [item for item in results if item.get("qualified")]
        excluded = [item for item in results if not item.get("qualified")]
        terminal_distribution = Counter(str(item.get("terminal_status") or "") for item in results)
        terminal_distribution = Counter(
            {status: int(terminal_distribution.get(status, 0)) for status in _TERMINAL_STATUSES}
        )
        completed_count = (
            terminal_distribution[SteadyTerminalStatus.EVALUATED_QUALIFIED.value]
            + terminal_distribution[SteadyTerminalStatus.EVALUATED_REJECTED.value]
        )
        evidence_funnel = {
            evidence_type: {
                "complete": sum(
                    1
                    for item in results
                    if (item.get("evidence_status") or {}).get(evidence_type) == "complete"
                ),
                "requested": len(results),
            }
            for evidence_type in ("price", "dividend", "financial", "sector", "history")
        }
        provider_failure_records = [
            item
            for item in results
            if item.get("terminal_status") == SteadyTerminalStatus.PROVIDER_FAILURE.value
        ]
        failed_diagnostics = [
            diagnostic
            for item in provider_failure_records
            for diagnostic in item.get("provider_diagnostics") or []
            if isinstance(diagnostic, dict) and diagnostic.get("status_category") != "ok"
        ]
        insufficient_records = [
            item
            for item in results
            if item.get("terminal_status") == SteadyTerminalStatus.INSUFFICIENT_EVIDENCE.value
        ]
        stats.update(
            {
                "deep_requested_count": len(results),
                "deep_evaluated_count": len(results),
                "completed_evaluation_count": completed_count,
                "qualified_count": len(candidates),
                "evaluated_rejected_count": terminal_distribution[
                    SteadyTerminalStatus.EVALUATED_REJECTED.value
                ],
                "data_insufficient_count": terminal_distribution[
                    SteadyTerminalStatus.INSUFFICIENT_EVIDENCE.value
                ],
                "success_count": completed_count,
                "provider_failure_count": terminal_distribution[
                    SteadyTerminalStatus.PROVIDER_FAILURE.value
                ],
                "unsupported_sector_model_count": terminal_distribution[
                    SteadyTerminalStatus.UNSUPPORTED_SECTOR_MODEL.value
                ],
                "internal_error_count": terminal_distribution[
                    SteadyTerminalStatus.INTERNAL_ERROR.value
                ],
                "terminal_status_distribution": dict(terminal_distribution),
                "evidence_funnel": evidence_funnel,
                "provider_failure_by_operation": dict(
                    sorted(Counter(str(value.get("operation") or "unknown") for value in failed_diagnostics).items())
                ),
                "provider_failure_by_provider": dict(
                    sorted(Counter(str(value.get("provider") or "unknown") for value in failed_diagnostics).items())
                ),
                "insufficient_by_evidence": dict(
                    sorted(
                        Counter(
                            str(issue)
                            for item in insufficient_records
                            for issue in item.get("evidence_issues") or []
                        ).items()
                    )
                ),
                "deep_failure_rate": (
                    round((len(results) - completed_count) / len(results), 4)
                    if results
                    else None
                ),
                "deep_success_rate": round(completed_count / len(results), 4) if results else None,
                "data_insufficient_by_reason": dict(
                    sorted(Counter(str(item.get("failure_code") or "unknown") for item in excluded).items())
                ),
                "qualified_sector_distribution": dict(
                    sorted(Counter(str(item.get("sector_model") or "unknown") for item in candidates).items())
                ),
            }
        )
        if not seeds:
            data_status = "valid_zero"
        elif completed_count == len(results) and not candidates:
            data_status = "valid_zero"
        elif completed_count == 0 and terminal_distribution[
            SteadyTerminalStatus.PROVIDER_FAILURE.value
        ] == len(results):
            data_status = "provider_unavailable"
        elif completed_count < len(results):
            data_status = "degraded"
        else:
            data_status = "complete"
        prefilter_count = int(stats.get("prefilter_eligible_count") or 0)
        deep_evaluated_count = len(results)
        unevaluated_count = max(prefilter_count - deep_evaluated_count, 0)
        is_exhaustive = unevaluated_count == 0
        selection_mode = (
            SELECTION_MODE_EXHAUSTIVE if is_exhaustive else SELECTION_MODE_FIXED_SHORTLIST
        )
        stats.update(
            {
                "selection_mode": selection_mode,
                "deep_budget": self.max_deep_evaluations,
                "unevaluated_count": unevaluated_count,
                "is_exhaustive": is_exhaustive,
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
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "as_of": evaluation_date.isoformat(),
            "mode": self.mode,
            "data_status": data_status,
            "selection_mode": selection_mode,
            "universe_count": len(universe),
            "prefilter_count": prefilter_count,
            "deep_budget": self.max_deep_evaluations,
            "deep_evaluated_count": deep_evaluated_count,
            "unevaluated_count": unevaluated_count,
            "is_exhaustive": is_exhaustive,
            "source": "沪深全市场股票索引 + 公开分红/行情/财务数据",
            "universe": {
                "market": "沪深A股",
                "count": len(universe),
                "source": universe_source,
                "complete": len(universe) >= MIN_UNIVERSE_SIZE,
            },
            "dividend_plan_periods": plan_periods,
            "screening_stats": stats,
            "evaluated_count": len(results),
            "qualified_count": len(candidates),
            "candidates": candidates,
            "excluded": excluded,
            "methodology": {
                "priority": "风险硬门槛优先，规则分仅对证据完整且可比较的低风险候选生成",
                "scope": "覆盖全部沪深 A 股；全市场先做分红与盈利预筛，再对高质量种子做深度风险评估",
                "preselection": "全市场轻量筛选后按行业分层进入可配置深评预算；页面明确展示各阶段数量",
                "income": "仅按已实施且除权除息日不晚于评估日的现金分红计算 TTM 股息率",
                "replay": "按交易日历验证完整年度覆盖后进行历史复权价格回放，不宣称无泄漏总回报",
                "limitations": "不预测未来分红；金融行业无专用监管证据、披露时点不明或数据不足时不纳入候选",
            },
        }


def build_steady_income_dataset(
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    data_manager: Any = None,
    market_source: Any = None,
    stock_index_path: Path | None = None,
    as_of: date | None = None,
    max_deep_evaluations: int = MAX_DEEP_EVALUATIONS,
    max_workers: int = MAX_WORKERS,
    mode: str = "live",
    data_manager_factory: Any = None,
    calendar: SessionCalendar | None = None,
) -> dict[str, Any]:
    source = market_source or PublicMarketSource(stock_index_path=stock_index_path)
    payload = SteadyIncomeDatasetBuilder(
        data_manager=data_manager,
        data_manager_factory=data_manager_factory,
        market_source=source,
        max_workers=max_workers,
        max_deep_evaluations=max_deep_evaluations,
        mode=mode,
        calendar=calendar,
    ).build(as_of=as_of)
    write_json_atomic(output_path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the whole-market rule-based steady-income Pages dataset")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--stock-index", type=Path, default=None)
    parser.add_argument("--max-deep-evaluations", type=int, default=MAX_DEEP_EVALUATIONS)
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--mode", choices=("live", "historical"), default="live")
    args = parser.parse_args(argv)
    try:
        payload = build_steady_income_dataset(
            output_path=args.output,
            stock_index_path=args.stock_index,
            max_deep_evaluations=args.max_deep_evaluations,
            max_workers=args.workers,
            mode=args.mode,
        )
    except Exception as exc:
        print(f"ERROR: steady-income dataset build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    stats = payload["screening_stats"]
    print(
        "Steady-income market dataset written: "
        f"{args.output} (universe={stats['universe_count']}, "
        f"prefilter={stats['prefilter_eligible_count']}, "
        f"deep={stats['deep_requested_count']}, completed={stats['completed_evaluation_count']}, "
        f"qualified={payload['qualified_count']})"
    )
    for item in list(payload.get("candidates") or []) + list(payload.get("excluded") or []):
        failed = [
            value
            for value in item.get("provider_diagnostics") or []
            if isinstance(value, dict) and value.get("status_category") != "ok"
        ]
        operations = ",".join(
            f"{value.get('provider')}/{value.get('operation')}/{value.get('status_category')}"
            for value in failed
        ) or "none"
        print(
            "deep diagnostic: "
            f"code={item.get('code')} sector={item.get('sector_model')} "
            f"terminal={item.get('terminal_status')} failure={item.get('failure_code')} "
            f"evidence={','.join(str(value) for value in item.get('evidence_issues') or []) or 'none'} "
            f"provider_failures={operations}"
        )
    print(f"terminal_status_distribution={json.dumps(stats['terminal_status_distribution'], sort_keys=True)}")
    print(f"provider_failure_by_operation={json.dumps(stats['provider_failure_by_operation'], sort_keys=True)}")
    print(f"provider_failure_by_provider={json.dumps(stats['provider_failure_by_provider'], sort_keys=True)}")
    print(f"insufficient_by_evidence={json.dumps(stats['insufficient_by_evidence'], sort_keys=True)}")
    print("LLM calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
