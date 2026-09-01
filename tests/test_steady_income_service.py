# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import threading
import time
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd

from src.services.steady_income_service import (
    SteadyIncomeService,
    _implemented_dividend_events,
    evaluate_steady_income_candidate,
)
from src.services.steady_income_contracts import SteadyIncomeProviderUnavailable


def _history_frame() -> pd.DataFrame:
    dates = pd.date_range("2020-01-02", "2025-12-31", freq="B")
    closes = [50.0 + index * 0.025 + math.sin(index / 23) * 0.5 for index in range(len(dates))]
    return pd.DataFrame({"date": dates, "close": closes})


def _context(*, dividend_yield: float = 4.2, operating_cash_flow: float = 130.0) -> dict:
    cash_per_share = 50.0 * dividend_yield / 100.0
    return {
        "security_master": {"industry": "公用事业", "source": "fixture"},
        "valuation": {"data": {"pe_ratio": 12.0, "pb_ratio": 1.5}},
        "growth": {"data": {"roe": 13.0, "net_profit_yoy": 8.0}},
        "earnings": {
            "data": {
                "financial_report": {
                    "period_end": "2026-06-30",
                    "available_at": "2026-08-20",
                    "net_profit_parent": 100.0,
                    "operating_cash_flow": operating_cash_flow,
                    "net_profit_period_end": "2026-06-30",
                    "operating_cash_flow_period_end": "2026-06-30",
                    "net_profit_unit": "CNY",
                    "operating_cash_flow_unit": "CNY",
                    "net_profit_flow_basis": "cumulative",
                    "operating_cash_flow_flow_basis": "cumulative",
                    "roe": 13.0,
                },
                "dividend": {
                    "ttm_dividend_yield_pct": dividend_yield,
                    "ttm_cash_dividend_per_share": cash_per_share,
                    "events": [
                        {
                            "event_date": f"{year}-06-20",
                            "ex_dividend_date": f"{year}-06-20",
                            "cash_dividend_per_share": cash_per_share,
                            "implemented": True,
                            "implementation_status": "implemented",
                        }
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


class IntradaySessionCalendar:
    def sessions_between(self, start: date, end: date) -> list[date]:
        return [value.date() for value in pd.date_range(start, end, freq="B")]

    def completed_session_at(self, moment: datetime) -> date:
        assert moment.tzinfo is not None
        return date(2026, 8, 28)


class ReentryDetectingManager:
    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def _enter(self) -> None:
        with self._state_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)

    def _exit(self) -> None:
        with self._state_lock:
            self.active -= 1

    def get_fundamental_context(self, code: str, budget_seconds: float) -> dict:
        self._enter()
        try:
            return _context()
        finally:
            self._exit()

    def get_daily_data(self, code: str, **_: object):
        self._enter()
        try:
            return _history_frame(), "mock"
        finally:
            self._exit()


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
        self.assertNotIn("price_bands", result)
        self.assertIsNotNone(result["ranking_score"])
        self.assertEqual(result["public_risk_label"], "规则低风险 A")

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
            {"event_date": "2024-06-20", "cash_dividend_per_share": 2.1, "implemented": True},
            {"event_date": "2025-06-20", "cash_dividend_per_share": 0, "implemented": True},
            {"event_date": "2026-12-20", "cash_dividend_per_share": 2.1, "implemented": True},
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

    def test_dividend_events_require_implementation_ex_date_and_are_deduplicated(self) -> None:
        events = _implemented_dividend_events(
            [
                {"ex_dividend_date": "2026-06-20", "cash_dividend_per_share": 1.2, "implemented": True},
                {"ex_dividend_date": "2026-06-20", "cash_dividend_per_share": 1.2, "implementation_status": "implemented"},
                {"announcement_date": "2026-05-01", "cash_dividend_per_share": 1.2, "implemented": True},
                {"ex_dividend_date": "2026-07-20", "cash_dividend_per_share": 1.2, "implementation_status": "proposal"},
                {"ex_dividend_date": "2027-06-20", "cash_dividend_per_share": 1.2, "implemented": True},
            ],
            date(2026, 8, 26),
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_date"], "2026-06-20")

    def test_dividend_streak_counts_years_not_events_or_revisions(self) -> None:
        context = _context()
        context["earnings"]["data"]["dividend"]["events"] = [
            {"plan_id": "2024-a", "ex_dividend_date": "2024-06-20", "cash_dividend_per_share": 1.0, "implemented": True},
            {"plan_id": "2024-b", "ex_dividend_date": "2024-12-20", "cash_dividend_per_share": 0.5, "implemented": True},
            {"plan_id": "2025-a", "ex_dividend_date": "2025-06-20", "cash_dividend_per_share": 1.1, "implemented": True},
            {"plan_id": "2025-a", "ex_dividend_date": "2025-06-20", "cash_dividend_per_share": 1.2, "implemented": True, "announcement_date": "2025-06-10"},
            {"plan_id": "2026-special", "ex_dividend_date": "2026-06-20", "cash_dividend_per_share": 1.3, "implemented": True},
        ]
        result = evaluate_steady_income_candidate(
            code="600010",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )
        self.assertEqual(result["consecutive_dividend_years"], 3)

        context["earnings"]["data"]["dividend"]["events"] = [
            event
            for event in context["earnings"]["data"]["dividend"]["events"]
            if "2025" not in str(event.get("ex_dividend_date"))
        ]
        broken = evaluate_steady_income_candidate(
            code="600010",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )
        self.assertEqual(broken["consecutive_dividend_years"], 1)

    def test_financial_flow_ratio_requires_same_period_basis_and_unit(self) -> None:
        mismatches = (
            ("operating_cash_flow_period_end", "2025-12-31"),
            ("operating_cash_flow_unit", "CNY_10K"),
            ("operating_cash_flow_flow_basis", "single_quarter"),
        )
        for field, value in mismatches:
            with self.subTest(field=field):
                context = _context()
                context["earnings"]["data"]["financial_report"][field] = value
                result = evaluate_steady_income_candidate(
                    code="600011",
                    current_price=50.0,
                    price_date="2026-08-26",
                    context=context,
                    history=_history_frame(),
                    as_of=date(2026, 8, 26),
                )
                self.assertIsNone(result["cash_flow_coverage_ratio"])
                self.assertFalse(result["qualified"])
                self.assertIn("财务流量期间/口径/单位一致性", "".join(result["risks"]))

        context = _context()
        context["earnings"]["data"]["financial_report"]["net_profit_unit"] = None
        missing_unit = evaluate_steady_income_candidate(
            code="600011",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )
        self.assertIsNone(missing_unit["cash_flow_coverage_ratio"])

    def test_live_mode_accepts_provider_reported_same_period_ratio_without_guessing_units(self) -> None:
        context = _context()
        financial = context["earnings"]["data"]["financial_report"]
        financial["available_at"] = None
        financial["announced_at"] = None
        financial["net_profit_unit"] = None
        financial["operating_cash_flow_unit"] = None
        financial["net_profit_flow_basis"] = None
        financial["operating_cash_flow_flow_basis"] = None
        financial["cash_flow_coverage_ratio"] = 1.3
        financial["cash_flow_coverage_period_end"] = "2026-06-30"
        financial["cash_flow_coverage_source"] = "provider_reported_same_period_ratio"

        live = evaluate_steady_income_candidate(
            code="600011",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
            mode="live",
        )
        self.assertEqual(live["cash_flow_coverage_ratio"], 1.3)
        self.assertNotIn("unverifiable_financial_flow_semantics", live["evidence_issues"])
        self.assertEqual(live["evidence"]["financial"]["evidence_mode"], "current_known_live")

        historical = evaluate_steady_income_candidate(
            code="600011",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
            mode="historical",
        )
        self.assertEqual(historical["terminal_status"], "insufficient_evidence")
        self.assertIn("missing_available_at", historical["evidence_issues"])

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

    def test_bank_never_uses_corporate_cash_flow_model_without_regulatory_evidence(self) -> None:
        context = _context()
        context["security_master"]["industry"] = "银行"
        result = evaluate_steady_income_candidate(
            code="600005",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
        )
        self.assertEqual(result["sector_model"], "bank")
        self.assertEqual(result["risk_tier"], "数据不足")
        self.assertFalse(result["qualified"])
        self.assertIsNone(result["ranking_score"])
        self.assertEqual(result["failure_code"], "unsupported_sector_model")
        self.assertNotIn("经营现金流/归母净利润", "".join(result["strengths"]))

    def test_historical_financial_evidence_requires_known_disclosure_time(self) -> None:
        context = _context()
        context["earnings"]["data"]["financial_report"]["available_at"] = "2026-09-01"
        result = evaluate_steady_income_candidate(
            code="600006",
            current_price=50.0,
            price_date="2026-08-26",
            context=context,
            history=_history_frame(),
            as_of=date(2026, 8, 26),
            mode="historical",
        )
        self.assertEqual(result["risk_tier"], "数据不足")
        self.assertIn("财务披露时点证据", "".join(result["risks"]))
        self.assertEqual(result["evidence"]["financial"]["period_end"], "2026-06-30")
        self.assertEqual(result["evidence"]["financial"]["available_at"], "2026-09-01")

    def test_future_or_stale_price_date_is_data_insufficient(self) -> None:
        for price_date, expected_reason in (
            ("2026-08-27", "行情日期晚于评估日"),
            ("2026-08-25", "行情日期早于最近应有交易日"),
        ):
            result = evaluate_steady_income_candidate(
                code="600007",
                current_price=50.0,
                price_date=price_date,
                context=_context(),
                history=_history_frame(),
                as_of=date(2026, 8, 26),
            )
            self.assertEqual(result["risk_tier"], "数据不足")
            self.assertIn(expected_reason, "".join(result["risks"]))

    def test_intraday_live_run_accepts_previous_completed_session_price(self) -> None:
        result = evaluate_steady_income_candidate(
            code="600007",
            current_price=50.0,
            price_date="2026-08-28",
            context=_context(),
            history=_history_frame(),
            as_of=date(2026, 8, 31),
            calendar=IntradaySessionCalendar(),
            evaluation_moment=datetime(
                2026,
                8,
                31,
                10,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        )
        self.assertNotIn("行情日期早于最近应有交易日", "".join(result["risks"]))

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

    def test_shared_injected_data_manager_is_not_reentered_by_workers(self) -> None:
        snapshot = {
            "as_of": "2026-08-26",
            "accounts": [
                {
                    "market": "cn",
                    "positions": [
                        {
                            "symbol": code,
                            "market": "cn",
                            "last_price": 50.0,
                            "price_date": "2026-08-26",
                        }
                        for code in ("600001", "600002", "000001")
                    ],
                }
            ],
        }
        manager = ReentryDetectingManager()
        service = SteadyIncomeService(
            portfolio_service=SimpleNamespace(get_portfolio_snapshot=lambda **_: snapshot),
            data_manager=manager,
        )

        response = service.evaluate_portfolio(refresh=True)

        self.assertEqual(response["evaluated_count"], 3)
        self.assertEqual(manager.max_active, 1)
        self.assertEqual(response["selection_mode"], "portfolio")
        self.assertTrue(response["is_exhaustive"])
        self.assertEqual(response["unevaluated_count"], 0)

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
            response = steady_income_endpoint.evaluate_portfolio(account_id=None, as_of=None, refresh=True)

        self.assertEqual(response.qualified_count, 1)
        self.assertEqual(response.candidates[0].risk_tier, "稳健")

    def test_api_http_semantics_and_typed_evidence_contract(self) -> None:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is not installed in the local Python runtime")
        from api.v1.endpoints import steady_income as endpoint

        app = FastAPI()
        app.include_router(endpoint.router, prefix="/steady")
        client = TestClient(app)

        invalid = client.get("/steady/portfolio", params={"as_of": "2026/08/26"})
        self.assertEqual(invalid.status_code, 422)

        complete_payload = SteadyIncomeService(
            portfolio_service=FakePortfolioService(), data_manager=FakeDataManager()
        ).evaluate_portfolio(refresh=True)
        with patch.object(
            endpoint,
            "_service",
            SimpleNamespace(evaluate_portfolio=lambda **_: complete_payload),
        ):
            response = client.get("/steady/portfolio", params={"as_of": "2026-08-26"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in (
            "schema_version",
            "model_version",
            "evaluator_version",
            "sector_model_version",
            "data_status",
        ):
            self.assertIn(key, body)
        self.assertIn("evidence", body["candidates"][0])
        self.assertEqual(body["candidates"][0]["sector_model"], "normal_corporate")

        insufficient_payload = SteadyIncomeService(
            portfolio_service=FakePortfolioService(), data_manager=FakeDataManager()
        ).evaluate_portfolio(refresh=True)
        insufficient_payload["candidates"] = []
        insufficient_payload["qualified_count"] = 0
        item = dict(complete_payload["candidates"][0])
        item.update(
            {
                "qualified": False,
                "ranking_score": None,
                "score": None,
                "risk_tier": "数据不足",
                "public_risk_label": "数据不足",
                "data_status": "数据不足",
                "failure_code": "insufficient_evidence",
            }
        )
        insufficient_payload["excluded"] = [item]
        insufficient_payload["data_status"] = "degraded"
        with patch.object(
            endpoint,
            "_service",
            SimpleNamespace(evaluate_portfolio=lambda **_: insufficient_payload),
        ):
            response = client.get("/steady/portfolio")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["excluded"][0]["data_status"], "数据不足")

        def unavailable(**_: object):
            raise SteadyIncomeProviderUnavailable("offline")

        with patch.object(endpoint, "_service", SimpleNamespace(evaluate_portfolio=unavailable)):
            self.assertEqual(client.get("/steady/portfolio").status_code, 503)

        def broken(**_: object):
            raise RuntimeError("bug")

        with patch.object(endpoint, "_service", SimpleNamespace(evaluate_portfolio=broken)):
            self.assertEqual(client.get("/steady/portfolio").status_code, 500)


if __name__ == "__main__":
    unittest.main()
