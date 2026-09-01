from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import build_stock_list_from_holdings as holdings
from scripts import check_report_coverage as coverage
from scripts import check_report_valid as report_valid
from scripts import update_advice_backtest as advice
from src.reports.contracts import HOLDINGS_SCHEMA_VERSION, REPORT_SCHEMA_VERSION
from src.reports.public_holdings import (
    build_public_holdings_snapshot,
    public_source_descriptor,
    stock_items_from_snapshot,
)


def _structured_report(*, success: bool = True) -> dict:
    results = [
        {
            "code": "111111", "name": "测试股票甲", "success": success,
            "failure_code": "none" if success else "provider_unavailable",
            "public_message": "" if success else "本标的模型服务暂不可用，未能完成。",
            "action_raw": "观望" if success else "",
            "action_normalized": "observe" if success else "unknown",
            "sentiment_raw": "震荡" if success else "",
            "sentiment_normalized": "neutral" if success else "unknown",
            "score": 50 if success else None, "public_summary": "结构化摘要。" if success else "", "sections": {},
        },
        {
            "code": "222222", "name": "测试股票乙", "success": False,
            "failure_code": "provider_unavailable", "public_message": "本标的模型服务暂不可用，未能完成。",
            "action_raw": "", "action_normalized": "unknown", "sentiment_raw": "",
            "sentiment_normalized": "unknown", "score": None, "public_summary": "", "sections": {},
        },
    ]
    success_ids = [item["code"] for item in results if item["success"]]
    failure_ids = [item["code"] for item in results if not item["success"]]
    return {
        "schema_version": REPORT_SCHEMA_VERSION, "run_id": "run-fixture",
        "generated_at": "2099-01-10T18:30:00+08:00", "market_data_as_of": "2099-01-09",
        "anchor_session": "2099-01-09", "report_date": "2099-01-10", "report_type": "stock_daily",
        "expected_stock_codes": ["111111", "222222"], "expected_count": 2,
        "success_count": len(success_ids), "failure_count": len(failure_ids),
        "success_ids": success_ids, "failure_ids": failure_ids,
        "status": "degraded" if success_ids else "failed", "results": results, "portfolio_reviews": [],
    }


class _DailyFrame:
    empty = False
    columns = ["日期", "收盘价"]

    def iterrows(self):
        yield 0, {"日期": "2026-06-18", "收盘价": "10.00"}
        yield 1, {"日期": "2026-06-19", "收盘价": "10.50"}


class _CountingManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_daily_data(self, code: str, **_: object):
        self.calls.append(code)
        return _DailyFrame(), "mock"


class HoldingReportWorkflowTests(unittest.TestCase):
    def test_snapshot_whitelists_fields_and_stock_list_keeps_all_accounts(self) -> None:
        raw = {"holdings": [
            {"account": "账户甲", "type": "stock", "code": "111111", "name": "测试股票", "shares": 10},
            {"account": "账户乙", "type": "A股", "code": "111111", "name": "测试股票", "unit_cost": 12.3},
            {"account": "账户乙", "type": "ETF", "code": "222222", "name": "测试场内基金", "market_value": 456},
            {"account": "账户丙", "type": "fund", "code": "333333", "name": "测试场外基金", "profit": 78},
            {"account": "账户丙", "type": "mystery", "code": "444444", "name": "未知类型"},
        ]}
        snapshot, warnings = build_public_holdings_snapshot(raw, r"C:\private\holdings.json")
        stocks = stock_items_from_snapshot(snapshot)

        self.assertEqual(snapshot["schema_version"], HOLDINGS_SCHEMA_VERSION)
        self.assertEqual(stocks, [{"code": "111111", "name": "测试股票", "type": "stock", "accounts": ["账户乙", "账户甲"]}])
        self.assertEqual(snapshot["source_kind"], "local_file")
        self.assertIsNone(snapshot["source_link"])
        self.assertNotIn("C:\\private", json.dumps(snapshot, ensure_ascii=False))
        self.assertTrue(any("unsupported enabled type" in item for item in warnings))
        for groups in snapshot["accounts"].values():
            for items in groups.values():
                for item in items:
                    self.assertEqual(set(item), {"account", "type", "name", "code"})

    def test_public_source_rejects_unsafe_url_schemes(self) -> None:
        for source in ("javascript:alert(1)", "file:///tmp/holdings.json", "data:text/plain,secret"):
            self.assertIsNone(public_source_descriptor(source, {"holdings": []})["source_link"])

    def test_public_source_drops_query_and_fragment_secrets(self) -> None:
        descriptor = public_source_descriptor(
            "https://api.github.com/repos/lwy13124975937-png/stock-dashboard/contents/holdings_data.json?token=secret#fragment",
            {"holdings": []},
        )
        self.assertEqual(
            descriptor["source_link"],
            "https://api.github.com/repos/lwy13124975937-png/stock-dashboard/contents/holdings_data.json",
        )
        self.assertNotIn("secret", json.dumps(descriptor))

    def test_holdings_log_url_never_contains_query_secret(self) -> None:
        self.assertEqual(
            holdings._safe_url_for_log("https://example.com/input.json?token=secret#part"),
            "https://example.com/input.json",
        )

    def test_actions_refuses_stock_list_without_full_snapshot(self) -> None:
        with patch.object(holdings, "_load_holdings_data", side_effect=RuntimeError("private source unavailable")):
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "STOCK_LIST": "111111"}, clear=False):
                self.assertEqual(holdings.build_stock_list(), "")

    def test_build_stock_list_writes_full_snapshot_and_stock_only_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_path = root / "holdings_data.json"
            site_data = root / "site_data"
            github_env = root / "github_env.txt"
            raw_path.write_text(json.dumps({"holdings": [
                {"account": "账户甲", "type": "stock", "code": "111111", "name": "测试股票"},
                {"account": "账户乙", "type": "lof", "code": "222222", "name": "测试基金"},
                {"account": "账户丙", "type": "otc", "code": "333333", "name": "测试场外基金"},
            ]}, ensure_ascii=False), encoding="utf-8")
            with (
                patch.object(holdings, "ROOT_DIR", root),
                patch.object(holdings, "SITE_DATA_DIR", site_data),
                patch.object(holdings, "SNAPSHOT_PATH", site_data / "holdings_snapshot.json"),
                patch.object(holdings, "CURRENT_STOCK_LIST_PATH", site_data / "current_stock_list.json"),
                patch.dict(os.environ, {"HOLDINGS_DATA_PATH": str(raw_path), "GITHUB_ENV": str(github_env), "GITHUB_ACTIONS": "true"}, clear=False),
            ):
                self.assertEqual(holdings.build_stock_list(), "111111")
            current = json.loads((site_data / "current_stock_list.json").read_text(encoding="utf-8"))
            snapshot = json.loads((site_data / "holdings_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual([item["code"] for item in current["stocks"]], ["111111"])
            self.assertEqual(set(snapshot["accounts"]), {"账户甲", "账户乙", "账户丙"})
            self.assertEqual(github_env.read_text(encoding="utf-8").strip(), "STOCK_LIST=111111")

    def test_coverage_uses_exact_structured_identity_including_failures(self) -> None:
        report = _structured_report()
        coverage.validate_coverage(["111111", "222222"], report)
        with self.assertRaises(coverage.CoverageValidationError):
            coverage.validate_coverage(["111111", "333333"], report)

    def test_report_validation_requires_structured_counts_and_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report_20990110.json"
            path.write_text(json.dumps(_structured_report(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(report_valid.validate_report_file(path)["success_count"], 1)
            path.write_text(json.dumps(_structured_report(success=False), ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                report_valid.validate_report_file(path)

    def test_price_history_is_cached_per_canonical_code(self) -> None:
        manager = _CountingManager()
        provider = advice.DataFetcherPriceProvider()
        provider._manager = manager
        first, first_error = provider.get_bars("SH600961", date(2026, 6, 18))
        second, second_error = provider.get_bars("600961.SH", date(2026, 6, 19))
        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual(first, second)
        self.assertEqual(manager.calls, ["600961"])

    def test_workflow_has_one_official_post_close_run_and_non_deploy_modes(self) -> None:
        workflow = (holdings.ROOT_DIR / ".github" / "workflows" / "00-daily-analysis.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count("cron:"), 1)
        self.assertIn("cron: '23 10 * * 1-5'", workflow)
        self.assertIn("pages-only", workflow)
        self.assertIn("validated-build-inputs", workflow)
        self.assertNotIn('echo "STOCK_LIST=', workflow)


if __name__ == "__main__":
    unittest.main()
