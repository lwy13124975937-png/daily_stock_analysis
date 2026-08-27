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
    SteadyIncomeDatasetBuilder,
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


def _snapshot() -> dict:
    return {
        "generated_at": "2026-08-26 20:00:00",
        "accounts": {
            "动态股票账户": {
                "stock": [
                    {"account": "动态股票账户", "type": "stock", "name": "测试银行", "code": "600001"}
                ],
                "lof": [
                    {"account": "动态股票账户", "type": "lof", "name": "测试LOF", "code": "160001"}
                ],
                "otc": [],
            },
            "动态基金账户": {
                "stock": [],
                "lof": [],
                "otc": [
                    {"account": "动态基金账户", "type": "otc", "name": "测试基金", "code": "012345"}
                ],
            },
        },
    }


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
    def test_dataset_uses_only_current_stock_holdings(self) -> None:
        manager = FakeDataManager()
        payload = SteadyIncomeDatasetBuilder(data_manager=manager).build(
            _snapshot(), as_of=date(2026, 8, 26)
        )

        self.assertEqual(manager.fundamental_codes, ["600001"])
        self.assertEqual(manager.history_codes, ["600001"])
        self.assertEqual(payload["evaluated_count"], 1)
        self.assertEqual(payload["qualified_count"], 1)
        self.assertEqual(payload["candidates"][0]["name"], "测试银行")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("160001", serialized)
        self.assertNotIn("012345", serialized)
        self.assertNotIn("LLM", serialized.upper())
        keys: set[str] = set()

        def collect_keys(value: object) -> None:
            if isinstance(value, dict):
                keys.update(str(key) for key in value)
                for child in value.values():
                    collect_keys(child)
            elif isinstance(value, list):
                for child in value:
                    collect_keys(child)

        collect_keys(payload)
        self.assertTrue(
            keys.isdisjoint({"unit_cost", "shares", "cost", "market_value", "profit", "amount", "total"})
        )

    def test_data_failure_never_becomes_low_risk_candidate(self) -> None:
        payload = SteadyIncomeDatasetBuilder(data_manager=FakeDataManager(fail=True)).build(
            _snapshot(), as_of=date(2026, 8, 26)
        )

        self.assertEqual(payload["qualified_count"], 0)
        self.assertEqual(payload["excluded"][0]["risk_tier"], "数据不足")
        self.assertFalse(payload["excluded"][0]["qualified"])

    def test_dataset_file_and_static_page_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_path = root / "holdings_snapshot.json"
            output_path = root / "steady_income.json"
            snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")

            payload = build_steady_income_dataset(
                snapshot_path=snapshot_path,
                output_path=output_path,
                data_manager=FakeDataManager(),
                as_of=date(2026, 8, 26),
            )
            html = build_pages_report._build_steady_income_page(payload)

            self.assertTrue(output_path.exists())
            self.assertIn("测试银行（600001）", html)
            self.assertIn("风险硬门槛优先", html)
            self.assertIn("不承诺收益", html)
            self.assertNotIn("测试LOF", html)
            self.assertNotIn("测试基金", html)
            for forbidden in ("unit_cost", "shares", "market_value", "持仓成本", "持仓市值", "盈亏"):
                self.assertNotIn(forbidden, html)

    def test_homepage_report_center_contains_steady_income_entry(self) -> None:
        html = build_pages_report._reports_index_block([])

        self.assertIn('href="steady_income.html"', html)
        self.assertIn("稳健收益", html)
        self.assertIn("低风险现金流", html)

    def test_html_contract_requires_exact_stock_coverage_and_risk_gate(self) -> None:
        manager = FakeDataManager()
        payload = SteadyIncomeDatasetBuilder(data_manager=manager).build(
            _snapshot(), as_of=date(2026, 8, 26)
        )
        html = build_pages_report._build_steady_income_page(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site_data = root / "site_data"
            site = root / "site"
            site_data.mkdir()
            site.mkdir()
            snapshot_path = site_data / "holdings_snapshot.json"
            data_path = site_data / "steady_income.json"
            page_path = site / "steady_income.html"
            snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
            data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            page_path.write_text(html, encoding="utf-8")

            with (
                patch.object(check_report_html, "HOLDINGS_SNAPSHOT_PATH", snapshot_path),
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
