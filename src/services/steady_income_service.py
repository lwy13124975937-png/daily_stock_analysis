# -*- coding: utf-8 -*-
"""Rule-based low-risk, steady-income evaluation for current A-share holdings.

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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd


RISK_TIER_ORDER = {
    "稳健": 0,
    "较稳健": 1,
    "观察": 2,
    "不纳入": 3,
    "数据不足": 4,
}
QUALIFIED_TIERS = {"稳健", "较稳健"}
CACHE_TTL_SECONDS = 6 * 60 * 60
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
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=["date", "close"])
    if "date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame(columns=["date", "close"])
    work = frame[["date", "close"]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    return (
        work.dropna(subset=["date", "close"])
        .loc[lambda item: item["close"] > 0]
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )


def _history_metrics(frame: Any, as_of: date) -> Dict[str, Any]:
    history = _normalize_history(frame)
    history = history.loc[history["date"].dt.date <= as_of].reset_index(drop=True)
    if len(history) < 2:
        return {
            "annualized_volatility_pct": None,
            "max_drawdown_pct": None,
            "replay_periods": [],
            "positive_replay_periods": 0,
        }

    closes = history["close"]
    returns = closes.pct_change().dropna()
    volatility = None
    if len(returns) >= 20:
        volatility = float(returns.std(ddof=0) * math.sqrt(252) * 100.0)
    drawdown = closes / closes.cummax() - 1.0
    max_drawdown = abs(float(drawdown.min() * 100.0)) if not drawdown.empty else None

    complete_years = history.loc[history["date"].dt.year < as_of.year].copy()
    year_ends: List[Dict[str, Any]] = []
    for year, group in complete_years.groupby(complete_years["date"].dt.year):
        if len(group) < 120:
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
        total_return = (current["close"] / previous["close"] - 1.0) * 100.0
        replay_periods.append(
            {
                "label": str(current["year"]),
                "start_date": previous["date"].isoformat(),
                "end_date": current["date"].isoformat(),
                "total_return_pct": round(total_return, 2),
            }
        )
    replay_periods = replay_periods[-5:]
    return {
        "annualized_volatility_pct": round(volatility, 2) if volatility is not None else None,
        "max_drawdown_pct": round(max_drawdown, 2) if max_drawdown is not None else None,
        "replay_periods": replay_periods,
        "positive_replay_periods": sum(1 for item in replay_periods if item["total_return_pct"] > 0),
    }


def _price_bands(ttm_cash: Optional[float]) -> Optional[Dict[str, float]]:
    if ttm_cash is None or ttm_cash <= 0:
        return None
    return {
        "high_income_price": round(ttm_cash / 0.05, 2),
        "balanced_price": round(ttm_cash / 0.035, 2),
        "low_income_price": round(ttm_cash / 0.025, 2),
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


def evaluate_steady_income_candidate(
    *,
    code: str,
    current_price: Optional[float],
    price_date: Optional[str],
    context: Dict[str, Any],
    history: Any,
    as_of: date,
) -> Dict[str, Any]:
    """Evaluate one stock using transparent rules and no predictive model."""

    growth = _block_data(context, "growth")
    valuation = _block_data(context, "valuation")
    earnings = _block_data(context, "earnings")
    dividend = earnings.get("dividend") if isinstance(earnings.get("dividend"), dict) else {}
    financial = earnings.get("financial_report") if isinstance(earnings.get("financial_report"), dict) else {}
    events = dividend.get("events") if isinstance(dividend.get("events"), list) else []

    ttm_cash = _safe_float(dividend.get("ttm_cash_dividend_per_share"))
    normalized_price = _safe_float(current_price)
    if normalized_price is not None and normalized_price <= 0:
        normalized_price = None
    ttm_yield = _safe_float(dividend.get("ttm_dividend_yield_pct"))
    if ttm_cash is not None and ttm_cash > 0 and normalized_price is not None:
        ttm_yield = ttm_cash / normalized_price * 100.0
    streak = _consecutive_dividend_years(events, as_of)
    net_profit = _safe_float(financial.get("net_profit_parent"))
    operating_cash_flow = _safe_float(financial.get("operating_cash_flow"))
    cash_flow_coverage = None
    if net_profit is not None and net_profit > 0 and operating_cash_flow is not None:
        cash_flow_coverage = operating_cash_flow / net_profit

    metrics = _history_metrics(history, as_of)
    volatility = metrics["annualized_volatility_pct"]
    max_drawdown = metrics["max_drawdown_pct"]
    replay_periods = metrics["replay_periods"]
    positive_periods = metrics["positive_replay_periods"]
    roe = _safe_float(growth.get("roe") or financial.get("roe"))
    profit_yoy = _safe_float(growth.get("net_profit_yoy"))
    pe_ratio = _safe_float(valuation.get("pe_ratio"))
    pb_ratio = _safe_float(valuation.get("pb_ratio"))

    essential_missing = []
    if normalized_price is None:
        essential_missing.append("当前价格")
    if not price_date:
        essential_missing.append("行情日期")
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
    if net_profit is None or operating_cash_flow is None:
        essential_missing.append("利润/经营现金流")

    hard_failures: List[str] = []
    if ttm_yield is not None and ttm_yield > 10:
        hard_failures.append("TTM 股息率超过 10%，需警惕高股息陷阱")
    if net_profit is not None and net_profit <= 0:
        hard_failures.append("最新归母净利润非正")
    if operating_cash_flow is not None and operating_cash_flow <= 0:
        hard_failures.append("最新经营现金流非正")
    if max_drawdown is not None and max_drawdown > 50:
        hard_failures.append("近年最大回撤超过 50%")
    if volatility is not None and volatility > 50:
        hard_failures.append("年化波动率超过 50%")
    if ttm_yield is not None and ttm_yield <= 0:
        hard_failures.append("近 12 个月没有可验证现金分红")

    sustainability = "偏弱"
    if streak >= 4 and cash_flow_coverage is not None and cash_flow_coverage >= 1.0:
        sustainability = "较强"
    elif streak >= 3 and cash_flow_coverage is not None and cash_flow_coverage >= 0.8:
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
    if cash_flow_coverage is not None:
        strengths.append(f"经营现金流/归母净利润 {cash_flow_coverage:.2f} 倍")
    if replay_periods:
        strengths.append(f"最近 {len(replay_periods)} 个完整年度中 {positive_periods} 个复权总回报为正")

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

    return {
        "code": code,
        "risk_tier": risk_tier,
        "qualified": risk_tier in QUALIFIED_TIERS,
        "score": min(int(round(score)), 100),
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
        "price_bands": _price_bands(ttm_cash),
        "strengths": strengths[:4],
        "risks": risks[:4],
        "data_status": "完整" if not essential_missing else "部分数据" if strengths else "数据不足",
    }


class SteadyIncomeService:
    """Evaluate current A-share portfolio holdings with a small runtime cache."""

    _cache_lock = threading.RLock()
    _cache: Dict[
        Tuple[Optional[int], str, Tuple[Tuple[str, Optional[float], str], ...]],
        Tuple[float, Dict[str, Any]],
    ] = {}

    def __init__(self, *, portfolio_service: Any = None, data_manager: Any = None) -> None:
        self._portfolio_service = portfolio_service
        self._data_manager = data_manager

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
        try:
            context = self.data_manager.get_fundamental_context(code, budget_seconds=8.0)
        except Exception as exc:
            warnings.append(f"基本面数据不可用：{type(exc).__name__}")
        try:
            history, provider = self.data_manager.get_daily_data(
                code,
                start_date=start_date,
                end_date=as_of.isoformat(),
                days=2000,
            )
            if provider:
                warnings.append(f"历史行情来源：{provider}")
        except Exception as exc:
            warnings.append(f"历史行情不可用：{type(exc).__name__}")

        result = evaluate_steady_income_candidate(
            code=code,
            current_price=position.get("current_price"),
            price_date=position.get("price_date"),
            context=context,
            history=history,
            as_of=as_of,
        )
        result["data_notes"] = warnings
        return result

    def evaluate_portfolio(self, *, account_id: Optional[int] = None, refresh: bool = False) -> Dict[str, Any]:
        snapshot = self.portfolio_service.get_portfolio_snapshot(account_id=account_id, cost_method="fifo")
        as_of_raw = snapshot.get("as_of") or date.today().isoformat()
        try:
            as_of = date.fromisoformat(str(as_of_raw)[:10])
        except ValueError:
            as_of = date.today()
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
        cache_key = (account_id, as_of.isoformat(), position_signature)
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
                                "code": code,
                                "risk_tier": "数据不足",
                                "qualified": False,
                                "score": 0,
                                "consecutive_dividend_years": 0,
                                "dividend_sustainability": "偏弱",
                                "positive_replay_periods": 0,
                                "replay_periods": [],
                                "strengths": [],
                                "risks": ["评估失败，未纳入稳健收益候选"],
                                "data_status": "数据不足",
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
        response = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": as_of.isoformat(),
            "source": "current_portfolio",
            "evaluated_count": len(results),
            "qualified_count": len(qualified),
            "candidates": qualified,
            "excluded": excluded,
            "warnings": warnings,
            "methodology": {
                "priority": "风险硬门槛优先，评分仅在同一风险层内排序",
                "dividend": "TTM 税前现金分红/当前持仓行情价格",
                "replay": "最近五个完整年度末之间的前复权总回报，已反映分红和拆并股影响",
                "price_bands": "按 TTM 每股现金分红分别倒推 5%、3.5%、2.5% 股息率价格",
                "limitations": "基于公开行情、分红和利润现金流证据；不预测未来分红，也不替代负债与派息政策核查",
            },
        }
        with self._cache_lock:
            self._cache[cache_key] = (time.time(), copy.deepcopy(response))
        return copy.deepcopy(response)
