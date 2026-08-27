#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the public, rule-based steady-income dataset for GitHub Pages.

This script deliberately uses only the sanitized holdings snapshot plus public
market and fundamental data. It never calls an LLM and never serializes
position quantities, costs, market values, or profit/loss fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.services.steady_income_service import (  # noqa: E402
    RISK_TIER_ORDER,
    evaluate_steady_income_candidate,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SNAPSHOT_PATH = ROOT_DIR / "site_data" / "holdings_snapshot.json"
DEFAULT_OUTPUT_PATH = ROOT_DIR / "site_data" / "steady_income.json"
MAX_WORKERS = 3


def _load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"holdings snapshot not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), dict):
        raise ValueError(f"invalid holdings snapshot: {path}")
    return payload


def _stock_holdings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect unique current stock holdings without exposing position values."""
    stocks: dict[str, dict[str, Any]] = {}
    accounts = snapshot.get("accounts") or {}
    for account_name, groups in accounts.items():
        if not isinstance(groups, dict):
            continue
        for item in groups.get("stock") or []:
            if not isinstance(item, dict) or str(item.get("type") or "stock").lower() != "stock":
                continue
            code = str(item.get("code") or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            name = str(item.get("name") or code).strip() or code
            account = str(item.get("account") or account_name).strip()
            current = stocks.setdefault(
                code,
                {"code": code, "name": name, "accounts": []},
            )
            if current["name"] == code and name != code:
                current["name"] = name
            if account and account not in current["accounts"]:
                current["accounts"].append(account)
    return [stocks[code] for code in sorted(stocks)]


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
    def __init__(self, data_manager: Any = None, *, max_workers: int = MAX_WORKERS) -> None:
        self._data_manager = data_manager
        self.max_workers = max(1, int(max_workers))

    @property
    def data_manager(self) -> Any:
        if self._data_manager is None:
            from data_provider.base import DataFetcherManager

            self._data_manager = DataFetcherManager()
        return self._data_manager

    def _evaluate(self, holding: dict[str, Any], as_of: date) -> dict[str, Any]:
        code = holding["code"]
        context: dict[str, Any] = {}
        history: Any = pd.DataFrame()
        notes: list[str] = []

        try:
            context = self.data_manager.get_fundamental_context(code, budget_seconds=8.0)
        except Exception as exc:
            notes.append(f"基本面数据不可用：{type(exc).__name__}")

        try:
            history, provider = self.data_manager.get_daily_data(
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
                "name": holding["name"],
                "accounts": list(holding["accounts"]),
                "data_notes": notes,
            }
        )
        return result

    def build(self, snapshot: dict[str, Any], *, as_of: date | None = None) -> dict[str, Any]:
        evaluation_date = as_of or datetime.now(SHANGHAI_TZ).date()
        holdings = _stock_holdings(snapshot)
        results: list[dict[str, Any]] = []

        if holdings:
            worker_count = min(self.max_workers, len(holdings))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="steady-pages") as pool:
                futures = {
                    pool.submit(self._evaluate, holding, evaluation_date): holding
                    for holding in holdings
                }
                for future in as_completed(futures):
                    holding = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(
                            {
                                "code": holding["code"],
                                "name": holding["name"],
                                "accounts": list(holding["accounts"]),
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
        return {
            "schema_version": 1,
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "as_of": evaluation_date.isoformat(),
            "source": "site_data/holdings_snapshot.json + public market/fundamental data",
            "evaluated_count": len(results),
            "qualified_count": len(candidates),
            "candidates": candidates,
            "excluded": excluded,
            "methodology": {
                "priority": "风险硬门槛优先，规则分仅在同一风险层内排序",
                "scope": "仅评估当前持仓中的沪深北 A 股股票，基金不参与",
                "income": "以 TTM 税前现金分红和最近有效收盘价计算股息率",
                "replay": "最近五个完整年度末之间的前复权总回报",
                "limitations": "不预测未来分红，不承诺收益；数据不足时不纳入低风险候选",
            },
        }


def build_steady_income_dataset(
    *,
    snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    data_manager: Any = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    snapshot = _load_snapshot(snapshot_path)
    payload = SteadyIncomeDatasetBuilder(data_manager=data_manager).build(snapshot, as_of=as_of)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the rule-based steady-income Pages dataset")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)
    try:
        payload = build_steady_income_dataset(snapshot_path=args.snapshot, output_path=args.output)
    except Exception as exc:
        print(f"ERROR: steady-income dataset build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        "Steady-income dataset written: "
        f"{args.output} (evaluated={payload['evaluated_count']}, qualified={payload['qualified_count']})"
    )
    print("LLM calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
