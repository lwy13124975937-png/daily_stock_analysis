# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.site.builder import _render_index, _render_steady
from src.site.validator import _check_steady
from src.services.steady_income_contracts import summarize_deep_evaluation_counts
from scripts.build_steady_income_report import (
    MAX_DEEP_EVALUATIONS,
    PublicMarketSource,
    SteadyIncomeDatasetBuilder,
    _build_deep_context,
    _dividend_evidence,
    _financial_evidence,
    _has_evidence_failure,
    _is_sh_sz_a_share,
    _prefilter_market,
    _selection_queue_sensitivity,
    _select_sector_stratified,
    build_steady_income_dataset,
)
from scripts.audit_steady_income_selection import _spearman, summarize_selection_sensitivity


def _history_frame() -> pd.DataFrame:
    dates = pd.date_range("2020-01-02", "2026-08-26", freq="B")
    closes = [50.0 + index * 0.018 + math.sin(index / 23) * 0.35 for index in range(len(dates))]
    return pd.DataFrame({"date": dates, "close": closes})


def _context() -> dict:
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
                    "operating_cash_flow": 135.0,
                    "net_profit_period_end": "2026-06-30",
                    "operating_cash_flow_period_end": "2026-06-30",
                    "net_profit_unit": "CNY",
                    "operating_cash_flow_unit": "CNY",
                    "net_profit_flow_basis": "cumulative",
                    "operating_cash_flow_flow_basis": "cumulative",
                    "roe": 13.0,
                },
                "dividend": {
                    "ttm_dividend_yield_pct": 4.0,
                    "ttm_cash_dividend_per_share": 2.4,
                    "events": [
                        {
                            "event_date": f"{year}-06-20",
                            "ex_dividend_date": f"{year}-06-20",
                            "cash_dividend_per_share": 2.4,
                            "implemented": True,
                            "implementation_status": "implemented",
                        }
                        for year in range(2022, 2027)
                    ],
                },
            }
        },
    }


def _universe() -> list[dict[str, str]]:
    return [
        {"code": "600001", "name": "测试银行", "market": "沪市", "industry": "公用事业"},
        {"code": "000002", "name": "测试公用", "market": "深市", "industry": "公用事业"},
        {"code": "300003", "name": "测试成长", "market": "深市", "industry": "电子"},
    ]


def _plans() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "代码": "600001",
                "名称": "测试银行",
                "现金分红-现金分红比例": 20.0,
                "现金分红-股息率": 0.04,
                "每股收益": 4.0,
                "方案进度": "实施分配",
                "最新公告日期": "2026-06-01",
                "所属行业": "公用事业",
            },
            {
                "代码": "000002",
                "名称": "测试公用",
                "现金分红-现金分红比例": 10.0,
                "现金分红-股息率": 0.035,
                "每股收益": 2.0,
                "方案进度": "实施分配",
                "最新公告日期": "2026-06-02",
                "所属行业": "公用事业",
            },
            {
                "代码": "300003",
                "名称": "测试成长",
                "现金分红-现金分红比例": 0.0,
                "现金分红-股息率": 0.0,
                "每股收益": 1.0,
                "方案进度": "不分配",
                "最新公告日期": "2026-06-03",
                "所属行业": "电子",
            },
            {
                "代码": "430001",
                "名称": "北交测试",
                "现金分红-现金分红比例": 30.0,
                "现金分红-股息率": 0.05,
                "每股收益": 5.0,
                "方案进度": "实施分配",
                "最新公告日期": "2026-06-04",
            },
        ]
    )


def _dividend_history() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"代码": "600001", "上市日期": "2000-01-01", "分红次数": 20},
            {"代码": "000002", "上市日期": "2001-01-01", "分红次数": 18},
            {"代码": "300003", "上市日期": "2012-01-01", "分红次数": 2},
            {"代码": "430001", "上市日期": "2010-01-01", "分红次数": 20},
        ]
    )


def _financial_abstract() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"选项": "常用指标", "指标": "归母净利润", "20260630": 100.0, "20251231": 180.0},
            {"选项": "常用指标", "指标": "经营现金流量净额", "20260630": 135.0, "20251231": 220.0},
            {"选项": "常用指标", "指标": "经营活动净现金/归属母公司的净利润", "20260630": 1.35, "20251231": 1.2222},
            {"选项": "常用指标", "指标": "净资产收益率(ROE)", "20260630": 13.0, "20251231": 16.0},
        ]
    )


def _dividend_detail() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "公告日期": f"{year}-06-10",
                "派息": 24.0,
                "进度": "实施",
                "除权除息日": f"{year}-06-20",
            }
            for year in range(2022, 2027)
        ]
    )


class FakeMarketSource:
    def __init__(self) -> None:
        self.fiscal_years: list[int] = []
        self.deep_codes: list[str] = []

    def load_universe(self):
        return _universe(), "mock:whole-sh-sz-market"

    def load_dividend_plans(self, fiscal_year: int):
        self.fiscal_years.append(fiscal_year)
        return _plans()

    def load_dividend_history(self):
        return _dividend_history()

    def load_deep_context(self, code: str, as_of: date):
        self.deep_codes.append(code)
        return _build_deep_context(
            _financial_abstract(),
            _dividend_detail(),
            as_of=as_of,
            industry="公用事业",
            financial_unit="CNY",
            flow_basis="cumulative",
        ), ["mock evidence"]


class FakeDataManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.fundamental_codes: list[str] = []
        self.history_codes: list[str] = []

    def get_fundamental_context(self, code: str, budget_seconds: float) -> dict:
        self.fundamental_codes.append(code)
        if self.fail:
            raise RuntimeError("offline")
        return _context()

    def get_daily_data(self, code: str, **_: object):
        self.history_codes.append(code)
        if self.fail:
            raise RuntimeError("offline")
        return _history_frame(), "mock"


class ThreadBoundManager(FakeDataManager):
    def __init__(self, registry: dict[int, set[int]], lock: object) -> None:
        super().__init__()
        self.registry = registry
        self.registry_lock = lock

    def get_daily_data(self, code: str, **kwargs: object):
        thread_id = __import__("threading").get_ident()
        with self.registry_lock:
            self.registry.setdefault(thread_id, set()).add(id(self))
        return super().get_daily_data(code, **kwargs)


class SteadyIncomePagesTests(unittest.TestCase):
    def test_bundled_universe_source_does_not_publish_an_absolute_path(self) -> None:
        index_path = Path(__file__).resolve().parents[1] / "apps" / "dsa-web" / "public" / "stocks.index.json"
        source = PublicMarketSource(stock_index_path=index_path)

        with patch("scripts.build_steady_income_report.requests.get", side_effect=RuntimeError("offline")):
            universe, source_label = source.load_universe()

        self.assertGreaterEqual(len(universe), 3000)
        self.assertEqual(source_label, "bundled:stocks.index.json")
        self.assertNotIn(str(index_path.parent), source_label)

    def test_market_code_filter_accepts_only_shanghai_and_shenzhen_a_shares(self) -> None:
        self.assertTrue(_is_sh_sz_a_share("600001", "600001.SH"))
        self.assertTrue(_is_sh_sz_a_share("000002", "000002.SZ"))
        self.assertTrue(_is_sh_sz_a_share("300003", "300003.SZ"))
        self.assertFalse(_is_sh_sz_a_share("430001", "430001.BJ"))
        self.assertFalse(_is_sh_sz_a_share("900901", "900901.SH"))
        self.assertFalse(_is_sh_sz_a_share("200002", "200002.SZ"))

    def test_whole_market_prefilter_is_independent_from_holdings(self) -> None:
        seeds, stats = _prefilter_market(
            _universe(),
            _plans(),
            _dividend_history(),
            as_of=date(2026, 8, 26),
        )

        self.assertEqual(stats["universe_count"], 3)
        self.assertEqual(stats["prefilter_eligible_count"], 2)
        self.assertEqual({item["code"] for item in seeds}, {"600001", "000002"})
        self.assertNotIn("300003", {item["code"] for item in seeds})
        self.assertNotIn("430001", {item["code"] for item in seeds})
        self.assertEqual(
            [row["deep_budget"] for row in stats["selection_sensitivity"]["budgets"]],
            [30, 60, 120],
        )

    def test_selection_sensitivity_is_offline_and_sector_stratified(self) -> None:
        eligible = [
            {
                "code": f"{index:06d}",
                "industry": f"行业{index % 5}",
                "seed_score": 200 - index,
            }
            for index in range(150)
        ]
        audit = _selection_queue_sensitivity(eligible)
        rows = {row["deep_budget"]: row for row in audit["budgets"]}
        self.assertEqual(rows[30]["selected_count"], 30)
        self.assertEqual(rows[60]["selected_count"], 60)
        self.assertEqual(rows[120]["selected_count"], 120)
        self.assertEqual(rows[120]["overlap_with_30_count"], 30)
        self.assertEqual(rows[120]["sector_coverage_count"], 5)
        self.assertIsNone(rows[120]["deep_failure_rate"])

    def test_sector_stratified_queue_is_deterministic_and_strictly_nested(self) -> None:
        eligible = [
            {
                "code": f"{index:06d}",
                "industry": f"行业{index % 7}",
                "seed_score": 100 - (index % 11),
            }
            for index in range(150)
        ]
        forward = _select_sector_stratified(eligible, limit=120)
        reverse = _select_sector_stratified(list(reversed(eligible)), limit=120)
        forward_codes = [item["code"] for item in forward]
        self.assertEqual(forward_codes, [item["code"] for item in reverse])
        self.assertEqual(
            [item["code"] for item in _select_sector_stratified(eligible, limit=30)],
            forward_codes[:30],
        )
        self.assertEqual(
            [item["code"] for item in _select_sector_stratified(eligible, limit=60)],
            forward_codes[:60],
        )

    def test_real_selection_audit_summary_compares_evaluated_budgets(self) -> None:
        records = []
        for position in range(1, 121):
            qualified = position in {1, 2, 31, 61}
            terminal_status = (
                "evaluated_qualified"
                if qualified
                else "provider_failure"
                if position % 7 == 0
                else "insufficient_evidence"
                if position % 5 == 0
                else "evaluated_rejected"
            )
            records.append(
                {
                    "code": f"{position:06d}",
                    "industry": f"行业{position % 4}",
                    "qualified": qualified,
                    "ranking_score": 100 - position if qualified else None,
                    "failure_code": (
                        "none" if terminal_status.startswith("evaluated_") else
                        "provider_unavailable" if terminal_status == "provider_failure" else
                        "insufficient_evidence"
                    ),
                    "terminal_status": terminal_status,
                    "preselection": {
                        "deep_queue_position": position,
                        "prefilter_position": position,
                        "seed_score": 100 - position / 10,
                    },
                }
            )
        audit = summarize_selection_sensitivity(
            {"as_of": "2026-08-26", "candidates": [item for item in records if item["qualified"]],
             "excluded": [item for item in records if not item["qualified"]],
             "screening_stats": {"universe_count": 5200, "prefilter_eligible_count": 700}},
        )
        rows = {row["deep_budget"]: row for row in audit["budgets"]}
        self.assertEqual(rows[30]["deep_evaluated"], 30)
        self.assertEqual(rows[60]["qualified_count"], 3)
        self.assertEqual(rows[120]["qualified_count"], 4)
        self.assertEqual(
            rows[120]["qualified_added_vs_first_budget"],
            ["000031", "000061"],
        )
        self.assertTrue(audit["queue_verification"]["strictly_nested"])
        self.assertEqual(len(audit["qualified_detail_reference"]), 4)
        self.assertEqual(
            audit["qualified_position_distribution"][:3],
            [
                {"positions": "1-30", "evaluated": True, "qualified_count": 2},
                {"positions": "31-60", "evaluated": True, "qualified_count": 1},
                {"positions": "61-120", "evaluated": True, "qualified_count": 1},
            ],
        )
        self.assertEqual(rows[30]["versus_reference"]["qualified_set_overlap"], 2)
        self.assertEqual(rows[30]["versus_reference"]["recall_of_reference_qualified"], 0.5)
        self.assertEqual(
            audit["prefilter_analysis"]["qualified_rate_by_deep_position_bucket"][0]["qualified_count"],
            2,
        )
        self.assertGreater(rows[120]["provider_failure_count"], 0)
        for row in rows.values():
            self.assertEqual(
                row["deep_evaluated"],
                row["qualified_count"]
                + row["evaluated_rejected_count"]
                + row["insufficient_evidence_count"]
                + row["unsupported_sector_model_count"]
                + row["provider_failure_count"]
                + row["internal_error_count"],
            )

    def test_selection_audit_rank_correlation_reranks_common_candidates(self) -> None:
        correlation = _spearman(
            ["baseline-a", "baseline-b", "baseline-c", "baseline-d"],
            ["new-x", "baseline-d", "new-y", "baseline-a", "baseline-b", "baseline-c"],
        )
        self.assertIsNotNone(correlation)
        self.assertGreaterEqual(correlation, -1.0)
        self.assertLessEqual(correlation, 1.0)

    def test_dataset_uses_one_data_manager_per_worker_when_factory_is_provided(self) -> None:
        import threading

        registry: dict[int, set[int]] = {}
        lock = threading.Lock()
        created: list[ThreadBoundManager] = []

        def factory() -> ThreadBoundManager:
            manager = ThreadBoundManager(registry, lock)
            created.append(manager)
            return manager

        payload = SteadyIncomeDatasetBuilder(
            data_manager_factory=factory,
            market_source=FakeMarketSource(),
            max_workers=2,
        ).build(as_of=date(2026, 8, 26))
        self.assertEqual(payload["evaluated_count"], 2)
        self.assertGreaterEqual(len(created), 1)
        self.assertTrue(all(len(manager_ids) == 1 for manager_ids in registry.values()))

    def test_public_market_source_uses_cninfo_sector_without_optional_eastmoney_failure(self) -> None:
        profile = pd.DataFrame([{"所属行业": "电气机械和器材制造业"}])
        source = PublicMarketSource()
        with (
            patch("akshare.stock_financial_abstract", return_value=_financial_abstract()),
            patch("akshare.stock_history_dividend_detail", return_value=_dividend_detail()),
            patch("akshare.stock_profile_cninfo", return_value=profile),
            patch("akshare.stock_individual_info_em") as eastmoney,
        ):
            context, _notes = source.load_deep_context("000521", date(2026, 8, 26))
        self.assertEqual(context["security_master"]["industry"], "电气机械和器材制造业")
        self.assertEqual(context["security_master"]["source"], "akshare.stock_profile_cninfo")
        self.assertFalse(any(value["status_category"] != "ok" for value in context["_provider_diagnostics"]))
        eastmoney.assert_not_called()

    def test_sector_fallback_failure_diagnostic_does_not_poison_completed_evaluation(self) -> None:
        source = PublicMarketSource()
        eastmoney_profile = pd.DataFrame(
            [{"item": "行业", "value": "公用事业"}]
        )
        with (
            patch("akshare.stock_financial_abstract", return_value=_financial_abstract()),
            patch("akshare.stock_history_dividend_detail", return_value=_dividend_detail()),
            patch("akshare.stock_profile_cninfo", side_effect=json.JSONDecodeError("bad", "", 0)),
            patch("akshare.stock_individual_info_em", return_value=eastmoney_profile),
        ):
            item = SteadyIncomeDatasetBuilder(
                data_manager=FakeDataManager(),
                market_source=source,
                max_workers=1,
                max_deep_evaluations=1,
            )._evaluate(
                {
                    "code": "600001", "name": "普通企业", "market": "沪市",
                    "industry": None, "deep_queue_position": 1, "seed_score": 90,
                    "plan_yield_pct": 4.0, "payout_ratio": 0.5, "dividend_count": 20,
                    "listing_years": 20.0, "plan_status": "实施分配",
                },
                date(2026, 8, 26),
            )
        self.assertIn(item["terminal_status"], {"evaluated_qualified", "evaluated_rejected"})
        self.assertEqual(item["provider_failures"], [])
        failed = [value for value in item["provider_diagnostics"] if value["status_category"] != "ok"]
        self.assertEqual(failed[0]["operation"], "stock_profile_cninfo")
        self.assertNotIn("url", json.dumps(failed).lower())
        self.assertNotIn("token", json.dumps(failed).lower())

    def test_known_financial_sectors_do_not_consume_normal_corporate_deep_budget(self) -> None:
        class SectorSource(FakeMarketSource):
            def load_sector(self, code: str):
                industry = {"600001": "银行业", "000002": "公用事业"}[code]
                return industry, "fixture", []

        eligible = [
            {"code": "600001", "name": "金融样本", "market": "沪市", "industry": None, "seed_score": 100},
            {"code": "000002", "name": "普通企业", "market": "深市", "industry": None, "seed_score": 90},
        ]
        builder = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(),
            market_source=SectorSource(),
            max_deep_evaluations=1,
        )
        selected, stats = builder._select_supported_seeds(eligible)
        self.assertEqual([item["code"] for item in selected], ["000002"])
        self.assertEqual(stats["predeep_unsupported_sector_model"], {"bank": 1})

    def test_deep_evidence_parses_indicator_layout_and_per_ten_dividends(self) -> None:
        growth, report = _financial_evidence(
            _financial_abstract(), as_of=date(2026, 8, 26), unit="CNY", flow_basis="cumulative"
        )
        dividend = _dividend_evidence(_dividend_detail(), as_of=date(2026, 8, 26))

        self.assertEqual(report["period_end"], "2026-06-30")
        self.assertEqual(report["net_profit_parent"], 100.0)
        self.assertEqual(report["operating_cash_flow"], 135.0)
        self.assertEqual(report["net_profit_unit"], "CNY")
        self.assertEqual(report["operating_cash_flow_period_end"], "2026-06-30")
        self.assertEqual(report["cash_flow_coverage_ratio"], 1.35)
        self.assertEqual(report["cash_flow_coverage_source"], "provider_reported_same_period_ratio")
        self.assertEqual(growth["roe"], 13.0)
        self.assertEqual(dividend["ttm_cash_dividend_per_share"], 2.4)
        self.assertEqual(len(dividend["events"]), 5)
        self.assertEqual(dividend["events"][0]["cash_dividend_per_share"], 2.4)

    def test_deep_evidence_never_uses_future_reports_or_unimplemented_dividends(self) -> None:
        financial = _financial_abstract().copy()
        financial["20261231"] = [999.0, 999.0, 9.99, 99.0]
        dividends = _dividend_detail().copy()
        dividends.loc[len(dividends)] = {
            "公告日期": "2026-08-20",
            "派息": 100.0,
            "进度": "预案",
            "除权除息日": "2026-09-20",
        }

        _growth, report = _financial_evidence(
            financial, as_of=date(2026, 8, 26), unit="CNY", flow_basis="cumulative"
        )
        dividend = _dividend_evidence(dividends, as_of=date(2026, 8, 26))

        self.assertEqual(report["net_profit_parent"], 100.0)
        self.assertEqual(dividend["ttm_cash_dividend_per_share"], 2.4)

    def test_historical_financial_visibility_uses_real_announcement_date(self) -> None:
        availability = {"20251231": "2026-03-28"}
        _growth, before = _financial_evidence(
            _financial_abstract(),
            as_of=date(2026, 2, 1),
            mode="historical",
            availability_by_period=availability,
            unit="CNY",
            flow_basis="cumulative",
        )
        self.assertEqual(before["evidence"]["status"], "evidence_unavailable")
        self.assertIsNone(before["evidence"].get("available_at"))

        _growth, after = _financial_evidence(
            _financial_abstract(),
            as_of=date(2026, 3, 29),
            mode="historical",
            availability_by_period=availability,
            unit="CNY",
            flow_basis="cumulative",
        )
        self.assertEqual(after["period_end"], "2025-12-31")
        self.assertEqual(after["announced_at"], "2026-03-28")
        self.assertEqual(after["available_at"], "2026-03-28")
        self.assertNotEqual(after["available_at"], after["evidence"]["fetched_at"][:10])

    def test_dividend_evidence_deduplicates_same_plan_revision_and_same_ex_date(self) -> None:
        rows = pd.DataFrame(
            [
                {"方案ID": "plan-a", "公告日期": "2026-05-01", "派息": 10.0, "进度": "实施", "除权除息日": "2026-06-20"},
                {"方案ID": "plan-a", "公告日期": "2026-05-10", "派息": 12.0, "进度": "实施", "除权除息日": "2026-06-20"},
                {"公告日期": "2026-05-11", "派息": 12.0, "进度": "实施", "除权除息日": "2026-06-20"},
            ]
        )
        evidence = _dividend_evidence(rows, as_of=date(2026, 8, 26))
        self.assertEqual(len(evidence["events"]), 1)
        self.assertEqual(evidence["ttm_cash_dividend_per_share"], 1.2)

    def test_deep240_count_contract_distinguishes_attempted_completed_and_unevaluated(self) -> None:
        counts = summarize_deep_evaluation_counts(
            prefilter_count=662,
            requested_count=240,
            terminal_distribution={
                "evaluated_qualified": 25,
                "evaluated_rejected": 206,
                "insufficient_evidence": 9,
                "unsupported_sector_model": 0,
                "provider_failure": 0,
                "internal_error": 0,
            },
        )
        self.assertEqual(counts["deep_requested_count"], 240)
        self.assertEqual(counts["deep_attempted_count"], 240)
        self.assertEqual(counts["deep_completed_count"], 231)
        self.assertEqual(counts["deep_evaluated_count"], 231)
        self.assertEqual(counts["insufficient_evidence_count"], 9)
        self.assertEqual(counts["unevaluated_count"], 422)
        self.assertEqual(
            counts["deep_attempted_count"],
            counts["qualified_count"] + counts["rejected_count"]
            + counts["insufficient_evidence_count"] + counts["unsupported_sector_count"]
            + counts["provider_failure_count"] + counts["internal_error_count"],
        )
        self.assertEqual(
            counts["deep_completed_count"],
            counts["qualified_count"] + counts["rejected_count"],
        )

    def test_dataset_evaluates_whole_market_seeds_not_current_holdings(self) -> None:
        manager = FakeDataManager()
        source = FakeMarketSource()
        payload = SteadyIncomeDatasetBuilder(data_manager=manager, market_source=source).build(
            as_of=date(2026, 8, 26)
        )

        self.assertEqual(manager.fundamental_codes, [])
        self.assertEqual(set(manager.history_codes), {"600001", "000002"})
        self.assertEqual(set(source.deep_codes), {"600001", "000002"})
        self.assertEqual(source.fiscal_years, [2025])
        self.assertEqual(payload["schema_version"], 6)
        self.assertEqual(payload["universe"]["market"], "沪深A股")
        self.assertIn("覆盖全部沪深 A 股", payload["methodology"]["scope"])
        self.assertNotIn("当前持仓", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["deep_requested_count"], 2)
        self.assertEqual(payload["deep_attempted_count"], 2)
        self.assertEqual(payload["deep_completed_count"], 2)
        self.assertEqual(payload["deep_evaluated_count"], 2)
        self.assertEqual(payload["evaluated_count"], 2)
        self.assertEqual(payload["qualified_count"], 2)
        self.assertEqual(MAX_DEEP_EVALUATIONS, 240)
        self.assertEqual(payload["selection_mode"], "exhaustive")
        self.assertTrue(payload["is_exhaustive"])
        self.assertEqual(payload["unevaluated_count"], 0)

    def test_data_failure_never_becomes_low_risk_candidate(self) -> None:
        payload = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(fail=True),
            market_source=FakeMarketSource(),
        ).build(as_of=date(2026, 8, 26))

        self.assertEqual(payload["qualified_count"], 0)
        self.assertTrue(all(item["risk_tier"] == "数据不足" for item in payload["excluded"]))
        self.assertTrue(all(not item["qualified"] for item in payload["excluded"]))
        self.assertEqual(payload["data_status"], "provider_unavailable")
        self.assertEqual(payload["screening_stats"]["provider_failure_count"], 2)

    def test_terminal_statuses_are_mutually_exclusive_and_success_means_completed(self) -> None:
        payload = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(),
            market_source=FakeMarketSource(),
        ).build(as_of=date(2026, 8, 26))
        stats = payload["screening_stats"]
        terminal = stats["terminal_status_distribution"]
        self.assertEqual(sum(terminal.values()), stats["deep_attempted_count"])
        self.assertGreaterEqual(stats["deep_requested_count"], stats["deep_attempted_count"])
        self.assertEqual(
            stats["deep_completed_count"],
            terminal["evaluated_qualified"] + terminal["evaluated_rejected"],
        )
        self.assertEqual(stats["deep_evaluated_count"], stats["deep_completed_count"])
        self.assertEqual(
            stats["success_count"],
            terminal["evaluated_qualified"] + terminal["evaluated_rejected"],
        )
        self.assertEqual(stats["provider_failure_count"], terminal["provider_failure"])
        self.assertEqual(stats["data_insufficient_count"], terminal["insufficient_evidence"])
        self.assertEqual(stats["completed_evaluation_count"], 2)

    def test_static_page_distinguishes_valid_zero_degraded_and_provider_outage(self) -> None:
        payload = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(),
            market_source=FakeMarketSource(),
        ).build(as_of=date(2026, 8, 26))
        payload["candidates"] = []
        payload["qualified_count"] = 0
        payload["screening_stats"]["qualified_count"] = 0
        payload["screening_stats"]["terminal_status_distribution"] = {
            "evaluated_qualified": 0,
            "evaluated_rejected": 2,
            "insufficient_evidence": 0,
            "unsupported_sector_model": 0,
            "provider_failure": 0,
            "internal_error": 0,
        }
        payload["screening_stats"]["rejected_count"] = 2
        payload["rejected_count"] = 2
        payload["screening_stats"]["completed_evaluation_count"] = 2
        payload["screening_stats"]["success_count"] = 2
        payload["screening_stats"]["prefilter_eligible_count"] = 3
        payload["screening_stats"]["unevaluated_count"] = 1
        payload["screening_stats"]["is_exhaustive"] = False
        payload["prefilter_count"] = 3
        payload["unevaluated_count"] = 1
        payload["is_exhaustive"] = False
        payload["selection_mode"] = "fixed_shortlist"
        payload["data_status"] = "valid_zero"
        html = _render_steady(payload, build_id="fixture-build")
        self.assertIn("个候选中没有满足全部硬条件的标的", html)
        self.assertIn("不能据此宣称全市场无合格标的", html)

        payload["screening_stats"]["terminal_status_distribution"] = {
            "evaluated_qualified": 0,
            "evaluated_rejected": 1,
            "insufficient_evidence": 0,
            "unsupported_sector_model": 0,
            "provider_failure": 1,
            "internal_error": 0,
        }
        payload["screening_stats"]["rejected_count"] = 1
        payload["screening_stats"]["provider_failure_count"] = 1
        payload["rejected_count"] = 1
        payload["provider_failure_count"] = 1
        payload["screening_stats"]["deep_completed_count"] = 1
        payload["screening_stats"]["deep_evaluated_count"] = 1
        payload["screening_stats"]["completed_evaluation_count"] = 1
        payload["screening_stats"]["success_count"] = 1
        payload["deep_completed_count"] = 1
        payload["deep_evaluated_count"] = 1
        payload["evaluated_count"] = 1
        payload["data_status"] = "degraded"
        html = _render_steady(payload, build_id="fixture-build")
        self.assertIn("仅完成完整规则判断 1/2", html)
        self.assertNotIn("合法的零候选结果", html)

        payload["screening_stats"]["terminal_status_distribution"] = {
            "evaluated_qualified": 0,
            "evaluated_rejected": 0,
            "insufficient_evidence": 0,
            "unsupported_sector_model": 0,
            "provider_failure": 2,
            "internal_error": 0,
        }
        payload["screening_stats"]["rejected_count"] = 0
        payload["screening_stats"]["provider_failure_count"] = 2
        payload["rejected_count"] = 0
        payload["provider_failure_count"] = 2
        payload["screening_stats"]["deep_completed_count"] = 0
        payload["screening_stats"]["deep_evaluated_count"] = 0
        payload["screening_stats"]["completed_evaluation_count"] = 0
        payload["screening_stats"]["success_count"] = 0
        payload["deep_completed_count"] = 0
        payload["deep_evaluated_count"] = 0
        payload["evaluated_count"] = 0
        payload["data_status"] = "provider_unavailable"
        html = _render_steady(payload, build_id="fixture-build")
        self.assertIn("数据源异常未能完成", html)
        self.assertNotIn("个候选中没有满足全部硬条件的标的", html)

    def test_evidence_failure_count_is_not_hidden_by_hard_exclusion_tier(self) -> None:
        self.assertTrue(
            _has_evidence_failure(
                {"risk_tier": "不纳入", "failure_code": "insufficient_evidence"}
            )
        )
        self.assertFalse(
            _has_evidence_failure({"risk_tier": "不纳入", "failure_code": "none"})
        )

    def test_valid_zero_and_provider_failure_are_distinct(self) -> None:
        class ZeroSource(FakeMarketSource):
            def load_dividend_plans(self, fiscal_year: int):
                self.fiscal_years.append(fiscal_year)
                plans = _plans().copy()
                plans["现金分红-股息率"] = 0.0
                return plans

        payload = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(), market_source=ZeroSource()
        ).build(as_of=date(2026, 8, 26))
        self.assertEqual(payload["data_status"], "valid_zero")
        self.assertEqual(payload["qualified_count"], 0)
        self.assertEqual(payload["evaluated_count"], 0)

        class BrokenSource(FakeMarketSource):
            def load_universe(self):
                raise RuntimeError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            SteadyIncomeDatasetBuilder(
                data_manager=FakeDataManager(), market_source=BrokenSource()
            ).build(as_of=date(2026, 8, 26))

    def test_historical_mode_refuses_non_point_in_time_provider(self) -> None:
        with self.assertRaisesRegex(Exception, "point-in-time"):
            SteadyIncomeDatasetBuilder(
                data_manager=FakeDataManager(),
                market_source=FakeMarketSource(),
                mode="historical",
            ).build(as_of=date(2025, 8, 26))

    def test_dataset_file_and_static_page_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "steady_income.json"
            payload = build_steady_income_dataset(
                output_path=output_path,
                data_manager=FakeDataManager(),
                market_source=FakeMarketSource(),
                as_of=date(2026, 8, 26),
            )
            html = _render_steady(payload, build_id="fixture-build")

            self.assertTrue(output_path.exists())
            self.assertIn("测试银行（600001）", html)
            self.assertIn("沪深全市场", html)
            self.assertIn("风险硬门槛优先", html)
            self.assertIn("不保证本金或收益", html)
            self.assertNotIn("当前 A 股持仓", html)
            self.assertNotIn("账户：", html)
            for forbidden in ("unit_cost", "shares", "market_value", "持仓成本", "持仓市值", "盈亏"):
                self.assertNotIn(forbidden, html)

            serialized = json.dumps(payload, ensure_ascii=False)
            for forbidden in ("account", "unit_cost", "shares", "cost", "market_value", "profit", "amount", "total"):
                self.assertNotIn(f'"{forbidden}"', serialized)

    def test_homepage_report_center_contains_whole_market_entry(self) -> None:
        html = _render_index(
            {"report_date": "2026-08-26"},
            {
                "generated_at": "2026-08-26T18:00:00+08:00",
                "source_label": "fixture",
                "source_link": None,
                "accounts": {},
            },
            market_pages=[],
            build_id="fixture-build",
            generated_at=datetime.fromisoformat("2026-08-26T18:05:00+08:00"),
        )

        self.assertIn('href="steady_income.html"', html)
        self.assertIn("稳健收益", html)
        self.assertIn("沪深全市场", html)
        self.assertIn("2026-08-26T18:05:00+08:00", html)

    def test_html_contract_requires_whole_market_funnel_and_risk_gate(self) -> None:
        payload = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(),
            market_source=FakeMarketSource(),
        ).build(as_of=date(2026, 8, 26))
        payload["universe"]["count"] = 5200
        payload["universe"]["complete"] = True
        payload["universe_count"] = 5200
        payload["screening_stats"]["universe_count"] = 5200
        html = _render_steady(payload, build_id="fixture-build")
        self.assertEqual(_check_steady(payload, html), [])

        payload["candidates"][0]["sector_model"] = "bank"
        errors = _check_steady(payload, html)
        self.assertTrue(any("financial sector" in error for error in errors))

    def test_daily_workflow_builds_dataset_before_pages(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "00-daily-analysis.yml").read_text(
            encoding="utf-8"
        )

        dataset_step = workflow.index("python scripts/build_steady_income_report.py")
        pages_step = workflow.index("python scripts/build_pages_report.py")
        html_check_step = workflow.index("python scripts/check_report_html.py")
        self.assertLess(dataset_step, pages_step)
        self.assertLess(pages_step, html_check_step)


if __name__ == "__main__":
    unittest.main()
