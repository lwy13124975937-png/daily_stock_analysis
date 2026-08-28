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
import json
import math
import re
import sys
import time
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
from src.services.steady_income_service import (  # noqa: E402
    RISK_TIER_ORDER,
    evaluate_steady_income_candidate,
)
from src.services.stock_index_remote_service import (  # noqa: E402
    DEFAULT_STOCK_INDEX_REMOTE_URL,
    validate_stock_index_payload,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_OUTPUT_PATH = ROOT_DIR / "site_data" / "steady_income.json"
MAX_WORKERS = 4
MAX_DEEP_EVALUATIONS = 30
GENERAL_PRESELECT_COUNT = 24
BANK_PRESELECT_COUNT = 8
MIN_PLAN_YIELD_PCT = 1.5
MAX_PLAN_YIELD_PCT = 8.0
MIN_DIVIDEND_COUNT = 5
MIN_LISTING_YEARS = 5.0
MIN_UNIVERSE_SIZE = 3000


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


class PublicMarketSource:
    """Load the whole-market index and bulk dividend pre-screen inputs."""

    def __init__(self, *, stock_index_path: Path | None = None) -> None:
        self.stock_index_path = stock_index_path

    def load_universe(self) -> tuple[list[dict[str, str]], str]:
        errors: list[str] = []
        try:
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

    def load_dividend_plans(self, fiscal_year: int) -> pd.DataFrame:
        import akshare as ak

        return _call_with_retry(
            lambda: ak.stock_fhps_em(date=f"{fiscal_year}1231"),
            attempts=2,
            label=f"{fiscal_year} annual dividend plans",
        )

    def load_dividend_history(self) -> pd.DataFrame:
        import akshare as ak

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
        try:
            financial = _call_with_retry(
                lambda: ak.stock_financial_abstract(symbol=code),
                attempts=2,
                label=f"{code} financial abstract",
            )
            notes.append("财务证据来源：AkShare 财务摘要")
        except Exception as exc:  # noqa: BLE001 - missing evidence must stay fail-closed.
            notes.append(f"财务证据不可用：{type(exc).__name__}")
        try:
            dividends = _call_with_retry(
                lambda: ak.stock_history_dividend_detail(symbol=code, indicator="分红", date=""),
                attempts=2,
                label=f"{code} dividend history",
            )
            notes.append("分红证据来源：AkShare 历史分红")
        except Exception as exc:  # noqa: BLE001 - missing evidence must stay fail-closed.
            notes.append(f"分红证据不可用：{type(exc).__name__}")
        return _build_deep_context(financial, dividends, as_of=as_of), notes


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


def _financial_evidence(frame: Any, *, as_of: date) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize AkShare's indicator-by-report-date financial abstract."""
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
        net_profit = _indicator_value(frame, ("归母净利润", "归属母公司股东净利润"), column)
        operating_cash_flow = _indicator_value(
            frame,
            ("经营现金流量净额", "经营活动产生的现金流量净额"),
            column,
        )
        if net_profit is None or operating_cash_flow is None:
            continue
        roe = _indicator_value(frame, ("净资产收益率(ROE)", "净资产收益率_ROE"), column)
        return (
            {"roe": roe} if roe is not None else {},
            {
                "report_date": report_date.isoformat(),
                "net_profit_parent": net_profit,
                "operating_cash_flow": operating_cash_flow,
                "roe": roe,
            },
        )
    return {}, {}


def _dividend_evidence(frame: Any, *, as_of: date) -> dict[str, Any]:
    """Normalize implemented cash dividends; AkShare's `派息` is per ten shares."""
    if not isinstance(frame, pd.DataFrame) or frame.empty or "派息" not in frame.columns:
        return {}
    events_by_key: dict[tuple[str, float], dict[str, Any]] = {}
    for _, row in frame.iterrows():
        progress = str(row.get("进度") or "").strip()
        if progress and "实施" not in progress:
            continue
        event_date = _date_from_any(row.get("除权除息日")) or _date_from_any(row.get("公告日期"))
        per_ten = _safe_float(row.get("派息"))
        if event_date is None or event_date > as_of or per_ten is None or per_ten <= 0:
            continue
        per_share = per_ten / 10.0
        key = (event_date.isoformat(), round(per_share, 6))
        events_by_key[key] = {
            "event_date": event_date.isoformat(),
            "ex_dividend_date": event_date.isoformat(),
            "cash_dividend_per_share": round(per_share, 6),
            "is_pre_tax": True,
        }
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
    }


def _build_deep_context(financial: Any, dividends: Any, *, as_of: date) -> dict[str, Any]:
    growth, financial_report = _financial_evidence(financial, as_of=as_of)
    dividend = _dividend_evidence(dividends, as_of=as_of)
    earnings: dict[str, Any] = {}
    if financial_report:
        earnings["financial_report"] = financial_report
    if dividend:
        earnings["dividend"] = dividend
    return {
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


def _prefilter_market(
    universe: list[dict[str, str]],
    plans: pd.DataFrame,
    dividend_history: pd.DataFrame,
    *,
    as_of: date,
    max_deep_evaluations: int = MAX_DEEP_EVALUATIONS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    required_plan_columns = {"代码", "名称", "现金分红-现金分红比例", "现金分红-股息率", "每股收益"}
    required_history_columns = {"代码", "上市日期", "分红次数"}
    if not isinstance(plans, pd.DataFrame) or plans.empty or not required_plan_columns.issubset(plans.columns):
        raise ValueError("annual dividend plan table is empty or missing required columns")
    if (
        not isinstance(dividend_history, pd.DataFrame)
        or dividend_history.empty
        or not required_history_columns.issubset(dividend_history.columns)
    ):
        raise ValueError("historical dividend summary is empty or missing required columns")

    universe_by_code = {item["code"]: item for item in universe}
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
        existing = latest_rows.get(code)
        current_date = _date_from_any(row.get("最新公告日期")) or date.min
        existing_date = _date_from_any(existing.get("最新公告日期")) if existing is not None else None
        if existing is None or current_date >= (existing_date or date.min):
            latest_rows[code] = row

    eligible: list[dict[str, Any]] = []
    for code, row in latest_rows.items():
        stock = universe_by_code[code]
        name = str(stock.get("name") or row.get("名称") or code).strip()
        if re.match(r"^(?:\*?ST|退)", name, flags=re.IGNORECASE):
            continue
        plan_yield = _safe_float(row.get("现金分红-股息率"))
        plan_dps_per_ten = _safe_float(row.get("现金分红-现金分红比例"))
        eps = _safe_float(row.get("每股收益"))
        if plan_yield is None or plan_dps_per_ten is None or eps is None:
            continue
        yield_pct = plan_yield * 100.0
        dps = plan_dps_per_ten / 10.0
        payout_ratio = dps / eps if eps > 0 else None
        history = history_by_code.get(code, {})
        dividend_count = int(history.get("dividend_count") or 0)
        listing_date = history.get("listing_date")
        listing_years = ((as_of - listing_date).days / 365.25) if isinstance(listing_date, date) else 0.0
        if not (
            MIN_PLAN_YIELD_PCT <= yield_pct <= MAX_PLAN_YIELD_PCT
            and dps > 0
            and eps > 0
            and payout_ratio is not None
            and 0.10 <= payout_ratio <= 0.90
            and dividend_count >= MIN_DIVIDEND_COUNT
            and listing_years >= MIN_LISTING_YEARS
        ):
            continue

        progress = str(row.get("方案进度") or "").strip()
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
    general = eligible[:GENERAL_PRESELECT_COUNT]
    banks = [item for item in eligible if "银行" in item["name"]][:BANK_PRESELECT_COUNT]
    selected_by_code = {item["code"]: item for item in general}
    for item in banks:
        selected_by_code.setdefault(item["code"], item)
    selected = sorted(
        selected_by_code.values(),
        key=lambda item: (-float(item["seed_score"]), -float(item["plan_yield_pct"]), item["code"]),
    )[: max(int(max_deep_evaluations), 1)]
    if not selected:
        raise RuntimeError("whole-market dividend pre-screen produced no deep-evaluation seeds")
    return selected, {
        "universe_count": len(universe),
        "annual_plan_count": len(latest_rows),
        "prefilter_eligible_count": len(eligible),
        "deep_selected_count": len(selected),
    }


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


class SteadyIncomeDatasetBuilder:
    def __init__(
        self,
        data_manager: Any = None,
        *,
        market_source: Any = None,
        max_workers: int = MAX_WORKERS,
        max_deep_evaluations: int = MAX_DEEP_EVALUATIONS,
    ) -> None:
        self._data_manager = data_manager
        self.market_source = market_source or PublicMarketSource()
        self.max_workers = max(1, int(max_workers))
        self.max_deep_evaluations = max(1, int(max_deep_evaluations))

    @property
    def data_manager(self) -> Any:
        if self._data_manager is None:
            from data_provider.base import DataFetcherManager

            self._data_manager = DataFetcherManager()
        return self._data_manager

    def _evaluate(self, seed: dict[str, Any], as_of: date, manager: Any) -> dict[str, Any]:
        code = seed["code"]
        context: dict[str, Any] = {}
        history: Any = pd.DataFrame()
        notes: list[str] = []

        try:
            loader = getattr(self.market_source, "load_deep_context")
            context, evidence_notes = loader(code, as_of)
            notes.extend(str(note) for note in evidence_notes if note)
        except Exception as exc:
            notes.append(f"稳健收益证据不可用：{type(exc).__name__}")

        try:
            history, provider = manager.get_daily_data(
                code,
                start_date=(as_of - timedelta(days=365 * 7)).isoformat(),
                end_date=as_of.isoformat(),
                days=2000,
            )
            if provider:
                notes.append(f"历史行情来源：{provider}")
        except Exception as exc:
            notes.append(f"历史行情不可用：{type(exc).__name__}")

        current_price, price_date = _latest_quote(history, as_of)
        result = evaluate_steady_income_candidate(
            code=code,
            current_price=current_price,
            price_date=price_date,
            context=context,
            history=history,
            as_of=as_of,
        )
        result.update(
            {
                "name": seed["name"],
                "market": seed["market"],
                "preselection": {
                    key: seed[key]
                    for key in (
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
        return result

    def build(self, *, as_of: date | None = None) -> dict[str, Any]:
        evaluation_date = as_of or datetime.now(SHANGHAI_TZ).date()
        fiscal_year = evaluation_date.year - 1
        universe, universe_source = self.market_source.load_universe()
        plans = self.market_source.load_dividend_plans(fiscal_year)
        dividend_history = self.market_source.load_dividend_history()
        seeds, stats = _prefilter_market(
            universe,
            plans,
            dividend_history,
            as_of=evaluation_date,
            max_deep_evaluations=self.max_deep_evaluations,
        )
        results: list[dict[str, Any]] = []

        # DataFetcherManager initializes several provider clients lazily. Build it
        # once on the main thread so concurrent candidate evaluation cannot race
        # through module imports or provider setup.
        manager = self.data_manager
        worker_count = min(self.max_workers, len(seeds))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="steady-market-pages") as pool:
            futures = {
                pool.submit(self._evaluate, seed, evaluation_date, manager): seed
                for seed in seeds
            }
            for future in as_completed(futures):
                seed = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(
                        {
                            "code": seed["code"],
                            "name": seed["name"],
                            "market": seed["market"],
                            "risk_tier": "数据不足",
                            "qualified": False,
                            "score": 0,
                            "current_price": None,
                            "price_date": None,
                            "ttm_dividend_yield_pct": None,
                            "consecutive_dividend_years": 0,
                            "dividend_sustainability": "偏弱",
                            "max_drawdown_pct": None,
                            "annualized_volatility_pct": None,
                            "positive_replay_periods": 0,
                            "replay_periods": [],
                            "price_bands": None,
                            "strengths": [],
                            "risks": ["公开数据不足，未纳入低风险候选"],
                            "data_status": "数据不足",
                            "preselection": {"seed_score": seed["seed_score"]},
                            "data_notes": [f"评估不可用：{type(exc).__name__}"],
                        }
                    )

        results.sort(
            key=lambda item: (
                RISK_TIER_ORDER.get(str(item.get("risk_tier")), 99),
                -int(item.get("score") or 0),
                str(item.get("code") or ""),
            )
        )
        candidates = [item for item in results if item.get("qualified")]
        excluded = [item for item in results if not item.get("qualified")]
        stats.update(
            {
                "deep_evaluated_count": len(results),
                "qualified_count": len(candidates),
                "data_insufficient_count": sum(item.get("risk_tier") == "数据不足" for item in results),
            }
        )
        return {
            "schema_version": 2,
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "as_of": evaluation_date.isoformat(),
            "source": "沪深全市场股票索引 + 公开分红/行情/财务数据",
            "universe": {
                "market": "沪深A股",
                "count": len(universe),
                "source": universe_source,
                "complete": len(universe) >= MIN_UNIVERSE_SIZE,
            },
            "screening_stats": stats,
            "evaluated_count": len(results),
            "qualified_count": len(candidates),
            "candidates": candidates,
            "excluded": excluded,
            "methodology": {
                "priority": "风险硬门槛优先，规则分仅在同一风险层内排序",
                "scope": "覆盖全部沪深 A 股；全市场先做分红与盈利预筛，再对高质量种子做深度风险评估",
                "preselection": "剔除 ST/退市风险、亏损、极端股息率、异常支付率、分红次数或上市年限不足的标的",
                "income": "以 TTM 税前现金分红和最近有效收盘价计算股息率",
                "replay": "最近五个完整年度末之间的前复权总回报",
                "limitations": "不预测未来分红，不承诺收益；预筛不是推荐，数据不足时不纳入低风险候选",
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
) -> dict[str, Any]:
    source = market_source or PublicMarketSource(stock_index_path=stock_index_path)
    payload = SteadyIncomeDatasetBuilder(
        data_manager=data_manager,
        market_source=source,
        max_deep_evaluations=max_deep_evaluations,
    ).build(as_of=as_of)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the whole-market rule-based steady-income Pages dataset")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--stock-index", type=Path, default=None)
    parser.add_argument("--max-deep-evaluations", type=int, default=MAX_DEEP_EVALUATIONS)
    args = parser.parse_args(argv)
    try:
        payload = build_steady_income_dataset(
            output_path=args.output,
            stock_index_path=args.stock_index,
            max_deep_evaluations=args.max_deep_evaluations,
        )
    except Exception as exc:
        print(f"ERROR: steady-income dataset build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    stats = payload["screening_stats"]
    print(
        "Steady-income market dataset written: "
        f"{args.output} (universe={stats['universe_count']}, "
        f"prefilter={stats['prefilter_eligible_count']}, "
        f"deep={stats['deep_evaluated_count']}, qualified={payload['qualified_count']})"
    )
    print("LLM calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
