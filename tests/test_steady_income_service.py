# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd

from src.services.steady_income_service import (
    SteadyIncomeService,
    evaluate_steady_income_candidate,
)


def _history_frame() -> pd.DataFrame:
    dates = pd.date_range("2020-01-02", "2025-12-31", freq="B")
    closes = [50.0 + index * 0.025 + math.sin(index / 23) * 0.5 for index in range(len(dates))]
    return pd.DataFrame({"date": dates, "close": closes})


def _context(*, dividend_yield: float = 4.2, operating_cash_flow: float = 130.0) -> dict:
    return {
        "valuation": {"data": {"pe_ratio": 12.0, "pb_ratio": 1.5}},
        "growth": {"data": {"roe": 13.0, "net_profit_yoy": 8.0}},
        "earnings": {
            "data": {
                "financial_report": {
                    "net_profit_parent": 100.0,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": 13.0,
                },
                "dividend": {
                    "ttm_dividend_yield_pct": dividend_yield,
                    "ttm_cash_dividend_per_share": 2.1,
                    "events": [
                        {"event_date": f"{year}-06-20", "cash_dividend_per_share": 2.1}
                        for year in range(2022, 2027)
                    ],
                },
            }
        },
    }


class FakePortfolioService:
    def __init__(self, price: float = 50.0) -> None:
        self.price = price

    def get_portfolio_snapshot(self, **_: object) -> dict:
        return {
            "as_of": "2026-08-26",
            "accounts": [
                {
                    "market": "cn",
                    "positions": [
                        {
                            "symbol": "SH600001",
                            "market": "cn",
                            "last_price": self.price,
                            "price_date": "2026-08-26",
                            "quantity": 200,
                            "avg_cost": 40.0,
                            "market_value_base": 10000.0,
                        },
                        {"symbol": "510300", "market": "cn", "last_price": 4.0},
                        {"symbol": "160105", "market": "cn", "last_price": 1.2},
                        {"symbol": "113001", "market": "cn", "last_price": 120.0},
                        {"symbol": "900901", "market": "cn", "last_price": 0.8},
                        {"symbol": "00700", "market": "hk", "last_price": 400.0},
                    ],
                }
            ],
        }


class FakeDataManager:
    def __init__(self) -> None:
        self.fundamental_calls = 0
        self.history_calls = 0

    def get_fundamental_context(self, code: str, budget_seconds: float) -> dict:
        assert code == "600001"
        assert budget_seconds == 8.0
        self.fundamental_calls += 1
        return _context()

    def get_daily_data(self, code: str, **_: object):
        assert code == "600001"
        self.history_calls += 1
        return _history_frame(), "mock"


class SteadyIncomeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        SteadyIncomeService._cache.clear()

    def test_low_risk_candidate_qualifies_before_score_ranking(self) -> None:
        result = evaluate_steady_income_candidate(
            code="600001",
            current_price=50.0,
            price_date="2026-08-26",
            context=_context(),
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )

        self.assertEqual(result["risk_tier"], "稳健")
        self.assertTrue(result["qualified"])
        self.assertEqual(result["consecutive_dividend_years"], 5)
        self.assertEqual(len(result["replay_periods"]), 5)
        self.assertEqual(result["replay_periods"][0]["label"], "2021")
        self.assertEqual(result["replay_periods"][0]["start_date"], "2020-12-31")
        self.assertEqual(result["price_bands"]["high_income_price"], 42.0)
        self.assertGreaterEqual(result["score"], 80)

    def test_high_yield_trap_is_excluded_even_with_high_score_inputs(self) -> None:
        result = evaluate_steady_income_candidate(
            code="600002",
            current_price=20.0,
            price_date="2026-08-26",
            context=_context(dividend_yield=12.0),
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )

        self.assertEqual(result["risk_tier"], "不纳入")
        self.assertFalse(result["qualified"])
        self.assertTrue(any("高股息陷阱" in item for item in result["risks"]))

    def test_short_history_is_data_insufficient_instead_of_stable(self) -> None:
        short_dates = pd.date_range("2025-07-01", "2025-12-31", freq="B")
        short_history = pd.DataFrame(
            {"date": short_dates, "close": [50 + index * 0.01 for index in range(len(short_dates))]}
        )

        result = evaluate_steady_income_candidate(
            code="600003",
            current_price=50.0,
            price_date="2026-08-26",
            context=_context(),
            history=short_history,
            as_of=date(2026, 8, 26),
        )

        self.assertEqual(result["risk_tier"], "数据不足")
        self.assertTrue(any("三年以上完整年度行情" in item for item in result["risks"]))

    def test_empty_typed_history_is_data_insufficient_instead_of_crashing(self) -> None:
        result = evaluate_steady_income_candidate(
            code="600003",
            current_price=None,
            price_date=None,
            context=_context(),
            history=pd.DataFrame(columns=["date", "close"]),
            as_of=date(2026, 8, 26),
        )

        self.assertEqual(result["risk_tier"], "数据不足")
        self.assertFalse(result["qualified"])
        self.assertTrue(any("长期行情" in item for item in result["risks"]))

    def test_future_and_zero_cash_events_do_not_extend_dividend_streak(self) -> None:
        context = _context()
        context["earnings"]["data"]["dividend"]["events"] = [
            {"event_date": "2024-06-20", "cash_dividend_per_share": 2.1},
            {"event_date": "2025-06-20", "cash_dividend_per_share": 0},
            {"event_date": "2026-12-20", "cash_dividend_per_share": 2.1},
        ]

        result = evaluate_steady_income_candidate(
            code="600003",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )

        self.assertEqual(result["consecutive_dividend_years"], 0)
        self.assertEqual(result["risk_tier"], "数据不足")
        self.assertTrue(any("现金分红记录" in item for item in result["risks"]))

    def test_missing_current_quote_cannot_qualify_from_provider_yield_alone(self) -> None:
        result = evaluate_steady_income_candidate(
            code="600004",
            current_price=None,
            price_date=None,
            context=_context(),
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )

        self.assertEqual(result["risk_tier"], "数据不足")
        self.assertFalse(result["qualified"])
        self.assertTrue(any("当前价格" in item and "行情日期" in item for item in result["risks"]))

    def test_portfolio_response_does_not_expose_sensitive_position_fields(self) -> None:
        service = SteadyIncomeService(
            portfolio_service=FakePortfolioService(),
            data_manager=FakeDataManager(),
        )
        response = service.evaluate_portfolio(refresh=True)

        self.assertEqual(response["evaluated_count"], 1)
        self.assertEqual(response["qualified_count"], 1)
        serialized = str(response)
        for forbidden in ("quantity", "avg_cost", "market_value_base", "unrealized_pnl"):
            self.assertNotIn(forbidden, serialized)

    def test_exchange_prefixed_a_share_is_normalized_and_evaluated(self) -> None:
        positions = SteadyIncomeService._collect_cn_positions(FakePortfolioService().get_portfolio_snapshot())

        self.assertEqual([item["code"] for item in positions], ["600001"])

    def test_all_equity_positions_are_kept_without_silent_count_truncation(self) -> None:
        snapshot = {
            "accounts": [
                {
                    "market": "cn",
                    "positions": [
                        {"symbol": f"60{index:04d}", "market": "cn", "last_price": 10.0}
                        for index in range(35)
                    ],
                }
            ]
        }

        positions = SteadyIncomeService._collect_cn_positions(snapshot)

        self.assertEqual(len(positions), 35)

    def test_service_uses_only_portfolio_and_market_data(self) -> None:
        response = SteadyIncomeService(
            portfolio_service=FakePortfolioService(),
            data_manager=FakeDataManager(),
        ).evaluate_portfolio(refresh=True)

        self.assertEqual(response["qualified_count"], 1)
        self.assertEqual(response["candidates"][0]["risk_tier"], "稳健")
        self.assertNotIn("llm", str(response).lower())

    def test_six_hour_cache_avoids_repeating_market_requests(self) -> None:
        manager = FakeDataManager()
        service = SteadyIncomeService(
            portfolio_service=FakePortfolioService(),
            data_manager=manager,
        )

        first = service.evaluate_portfolio(refresh=True)
        second = service.evaluate_portfolio()

        self.assertEqual(first, second)
        self.assertEqual(manager.fundamental_calls, 1)
        self.assertEqual(manager.history_calls, 1)

    def test_cached_response_is_not_mutated_by_a_previous_caller(self) -> None:
        manager = FakeDataManager()
        service = SteadyIncomeService(
            portfolio_service=FakePortfolioService(),
            data_manager=manager,
        )

        first = service.evaluate_portfolio(refresh=True)
        first["candidates"][0]["risk_tier"] = "不纳入"
        second = service.evaluate_portfolio()

        self.assertEqual(second["candidates"][0]["risk_tier"], "稳健")
        self.assertEqual(manager.fundamental_calls, 1)

    def test_cache_is_invalidated_when_portfolio_quote_changes(self) -> None:
        manager = FakeDataManager()
        portfolio = FakePortfolioService()
        service = SteadyIncomeService(portfolio_service=portfolio, data_manager=manager)

        service.evaluate_portfolio(refresh=True)
        portfolio.price = 52.0
        result = service.evaluate_portfolio()

        self.assertEqual(manager.fundamental_calls, 2)
        self.assertEqual(manager.history_calls, 2)
        self.assertEqual(result["candidates"][0]["current_price"], 52.0)

    def test_endpoint_returns_typed_response_when_fastapi_is_available(self) -> None:
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("FastAPI is not installed in the local Python runtime")

        from api.v1.endpoints import steady_income as steady_income_endpoint

        payload = SteadyIncomeService(
            portfolio_service=FakePortfolioService(),
            data_manager=FakeDataManager(),
        ).evaluate_portfolio(refresh=True)
        fake_service = SimpleNamespace(evaluate_portfolio=lambda **_: payload)

        with patch.object(steady_income_endpoint, "_service", fake_service):
            response = steady_income_endpoint.evaluate_portfolio(account_id=None, refresh=True)

        self.assertEqual(response.qualified_count, 1)
        self.assertEqual(response.candidates[0].risk_tier, "稳健")


if __name__ == "__main__":
    unittest.main()
