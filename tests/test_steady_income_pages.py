# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts import build_pages_report
from scripts import check_report_html
from scripts.build_steady_income_report import (
    PublicMarketSource,
    SteadyIncomeDatasetBuilder,
    _build_deep_context,
    _dividend_evidence,
    _financial_evidence,
    _is_sh_sz_a_share,
    _prefilter_market,
    build_steady_income_dataset,
)


def _history_frame() -> pd.DataFrame:
    dates = pd.date_range("2020-01-02", "2026-08-26", freq="B")
    closes = [50.0 + index * 0.018 + math.sin(index / 23) * 0.35 for index in range(len(dates))]
    return pd.DataFrame({"date": dates, "close": closes})


def _context() -> dict:
    return {
        "valuation": {"data": {"pe_ratio": 12.0, "pb_ratio": 1.5}},
        "growth": {"data": {"roe": 13.0, "net_profit_yoy": 8.0}},
        "earnings": {
            "data": {
                "financial_report": {
                    "net_profit_parent": 100.0,
                    "operating_cash_flow": 135.0,
                    "roe": 13.0,
                },
                "dividend": {
                    "ttm_dividend_yield_pct": 4.0,
                    "ttm_cash_dividend_per_share": 2.4,
                    "events": [
                        {"event_date": f"{year}-06-20", "cash_dividend_per_share": 2.4}
                        for year in range(2022, 2027)
                    ],
                },
            }
        },
    }


def _universe() -> list[dict[str, str]]:
    return [
        {"code": "600001", "name": "测试银行", "market": "沪市"},
        {"code": "000002", "name": "测试公用", "market": "深市"},
        {"code": "300003", "name": "测试成长", "market": "深市"},
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
            },
            {
                "代码": "000002",
                "名称": "测试公用",
                "现金分红-现金分红比例": 10.0,
                "现金分红-股息率": 0.035,
                "每股收益": 2.0,
                "方案进度": "实施分配",
                "最新公告日期": "2026-06-02",
            },
            {
                "代码": "300003",
                "名称": "测试成长",
                "现金分红-现金分红比例": 0.0,
                "现金分红-股息率": 0.0,
                "每股收益": 1.0,
                "方案进度": "不分配",
                "最新公告日期": "2026-06-03",
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
        return _build_deep_context(_financial_abstract(), _dividend_detail(), as_of=as_of), ["mock evidence"]


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

    def test_deep_evidence_parses_indicator_layout_and_per_ten_dividends(self) -> None:
        growth, report = _financial_evidence(_financial_abstract(), as_of=date(2026, 8, 26))
        dividend = _dividend_evidence(_dividend_detail(), as_of=date(2026, 8, 26))

        self.assertEqual(report["report_date"], "2026-06-30")
        self.assertEqual(report["net_profit_parent"], 100.0)
        self.assertEqual(report["operating_cash_flow"], 135.0)
        self.assertEqual(growth["roe"], 13.0)
        self.assertEqual(dividend["ttm_cash_dividend_per_share"], 2.4)
        self.assertEqual(len(dividend["events"]), 5)
        self.assertEqual(dividend["events"][0]["cash_dividend_per_share"], 2.4)

    def test_deep_evidence_never_uses_future_reports_or_unimplemented_dividends(self) -> None:
        financial = _financial_abstract().copy()
        financial["20261231"] = [999.0, 999.0, 99.0]
        dividends = _dividend_detail().copy()
        dividends.loc[len(dividends)] = {
            "公告日期": "2026-08-20",
            "派息": 100.0,
            "进度": "预案",
            "除权除息日": "2026-09-20",
        }

        _growth, report = _financial_evidence(financial, as_of=date(2026, 8, 26))
        dividend = _dividend_evidence(dividends, as_of=date(2026, 8, 26))

        self.assertEqual(report["net_profit_parent"], 100.0)
        self.assertEqual(dividend["ttm_cash_dividend_per_share"], 2.4)

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
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["universe"]["market"], "沪深A股")
        self.assertIn("覆盖全部沪深 A 股", payload["methodology"]["scope"])
        self.assertNotIn("当前持仓", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["evaluated_count"], 2)
        self.assertEqual(payload["qualified_count"], 2)

    def test_data_failure_never_becomes_low_risk_candidate(self) -> None:
        payload = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(fail=True),
            market_source=FakeMarketSource(),
        ).build(as_of=date(2026, 8, 26))

        self.assertEqual(payload["qualified_count"], 0)
        self.assertTrue(all(item["risk_tier"] == "数据不足" for item in payload["excluded"]))
        self.assertTrue(all(not item["qualified"] for item in payload["excluded"]))

    def test_dataset_file_and_static_page_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "steady_income.json"
            payload = build_steady_income_dataset(
                output_path=output_path,
                data_manager=FakeDataManager(),
                market_source=FakeMarketSource(),
                as_of=date(2026, 8, 26),
            )
            html = build_pages_report._build_steady_income_page(payload)

            self.assertTrue(output_path.exists())
            self.assertIn("测试银行（600001）", html)
            self.assertIn("沪深全市场", html)
            self.assertIn("风险硬门槛优先", html)
            self.assertIn("不承诺收益", html)
            self.assertNotIn("当前 A 股持仓", html)
            self.assertNotIn("账户：", html)
            for forbidden in ("unit_cost", "shares", "market_value", "持仓成本", "持仓市值", "盈亏"):
                self.assertNotIn(forbidden, html)

            serialized = json.dumps(payload, ensure_ascii=False)
            for forbidden in ("account", "unit_cost", "shares", "cost", "market_value", "profit", "amount", "total"):
                self.assertNotIn(f'"{forbidden}"', serialized)

    def test_homepage_report_center_contains_whole_market_entry(self) -> None:
        html = build_pages_report._reports_index_block([])

        self.assertIn('href="steady_income.html"', html)
        self.assertIn("稳健收益", html)
        self.assertIn("沪深全市场", html)

    def test_html_contract_requires_whole_market_funnel_and_risk_gate(self) -> None:
        payload = SteadyIncomeDatasetBuilder(
            data_manager=FakeDataManager(),
            market_source=FakeMarketSource(),
        ).build(as_of=date(2026, 8, 26))
        payload["universe"]["count"] = 5200
        payload["universe"]["complete"] = True
        payload["screening_stats"]["universe_count"] = 5200
        html = build_pages_report._build_steady_income_page(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site_data = root / "site_data"
            site = root / "site"
            site_data.mkdir()
            site.mkdir()
            data_path = site_data / "steady_income.json"
            page_path = site / "steady_income.html"
            data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            page_path.write_text(html, encoding="utf-8")

            with (
                patch.object(check_report_html, "STEADY_INCOME_DATA_PATH", data_path),
                patch.object(check_report_html, "STEADY_INCOME_PAGE_PATH", page_path),
            ):
                errors: list[str] = []
                check_report_html._check_steady_income_contract(
                    errors,
                    '<a href="steady_income.html">稳健收益</a>',
                )
                self.assertEqual(errors, [])

                payload["candidates"][0]["risk_tier"] = "观察"
                data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                check_report_html._check_steady_income_contract(
                    errors,
                    '<a href="steady_income.html">稳健收益</a>',
                )
                self.assertTrue(any("bypasses the low-risk tier gate" in error for error in errors))

                errors.clear()
                payload["candidates"][0]["risk_tier"] = "稳健"
                payload["candidates"][0]["code"] = "430001"
                data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                check_report_html._check_steady_income_contract(
                    errors,
                    '<a href="steady_income.html">稳健收益</a>',
                )
                self.assertTrue(any("malformed stock results" in error for error in errors))

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
