from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts import build_pages_report as pages
from scripts import build_stock_list_from_holdings as holdings
from scripts import check_report_coverage as coverage
from scripts import check_report_html as html_check
from scripts import check_report_valid as report_valid
from scripts import update_advice_backtest as advice


class _DailyFrame:
    empty = False
    columns = ["日期", "收盘价"]

    def iterrows(self):
        yield 0, {"日期": "2026-06-18", "收盘价": "10.00"}
        yield 1, {"日期": "2026-06-19", "收盘价": "10.50"}


class _CountingManager:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kwargs: list[dict] = []

    def get_daily_data(self, code: str, **kwargs):
        self.calls.append(code)
        self.kwargs.append(kwargs)
        return _DailyFrame(), "mock"


class HoldingReportWorkflowTests(unittest.TestCase):
    def test_snapshot_keeps_all_public_holdings_but_stock_list_is_stock_only(self) -> None:
        raw = {
            "holdings": [
                {
                    "account": "账户甲",
                    "type": "stock",
                    "code": "111111",
                    "name": "测试股票",
                    "shares": 10,
                    "unit_cost": 12.3,
                },
                {
                    "account": "账户乙",
                    "type": "ETF",
                    "code": "222222",
                    "name": "测试场内基金",
                    "market_value": 456,
                },
                {
                    "account": "账户丙",
                    "type": "fund",
                    "code": "333333",
                    "name": "测试场外基金",
                    "profit": 78,
                },
            ]
        }

        snapshot, type_codes, stock_list = holdings.build_holdings_snapshot(raw, "test:fixture")

        self.assertEqual(stock_list, ["111111"])
        self.assertEqual(
            type_codes,
            {"stock": ["111111"], "lof": ["222222"], "otc": ["333333"]},
        )
        self.assertEqual(set(snapshot["accounts"]), {"账户甲", "账户乙", "账户丙"})
        public_items = [
            item
            for groups in snapshot["accounts"].values()
            for items in groups.values()
            for item in items
        ]
        self.assertEqual(len(public_items), 3)
        self.assertTrue(
            all(set(item) == {"account", "type", "name", "code"} for item in public_items)
        )

    def test_actions_refuses_stock_list_only_without_full_snapshot(self) -> None:
        with patch.object(holdings, "_load_holdings_data", side_effect=RuntimeError("private source unavailable")):
            with patch.dict(
                os.environ,
                {"GITHUB_ACTIONS": "true", "STOCK_LIST": "111111,222222"},
                clear=False,
            ):
                self.assertEqual(holdings.build_stock_list(), "")

    def test_build_stock_list_writes_analysis_list_and_full_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_path = root / "holdings_data.json"
            site_data_dir = root / "site_data"
            snapshot_path = site_data_dir / "holdings_snapshot.json"
            stock_list_path = site_data_dir / "current_stock_list.json"
            github_env = root / "github_env.txt"
            raw_path.write_text(
                json.dumps(
                    {
                        "holdings": [
                            {"account": "账户甲", "type": "stock", "code": "111111", "name": "测试股票"},
                            {"account": "账户乙", "type": "lof", "code": "222222", "name": "测试场内基金"},
                            {"account": "账户丙", "type": "otc", "code": "333333", "name": "测试场外基金"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with ExitStack() as stack:
                stack.enter_context(patch.object(holdings, "ROOT_DIR", root))
                stack.enter_context(patch.object(holdings, "SITE_DATA_DIR", site_data_dir))
                stack.enter_context(patch.object(holdings, "SNAPSHOT_PATH", snapshot_path))
                stack.enter_context(patch.object(holdings, "CURRENT_STOCK_LIST_PATH", stock_list_path))
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "HOLDINGS_DATA_PATH": str(raw_path),
                            "GITHUB_ENV": str(github_env),
                            "GITHUB_ACTIONS": "true",
                        },
                        clear=False,
                    )
                )
                self.assertEqual(holdings.build_stock_list(), "111111")

            current_payload = json.loads(stock_list_path.read_text(encoding="utf-8"))
            snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual([item["code"] for item in current_payload["stocks"]], ["111111"])
            self.assertEqual(set(snapshot_payload["accounts"]), {"账户甲", "账户乙", "账户丙"})
            self.assertEqual(github_env.read_text(encoding="utf-8").strip(), "STOCK_LIST=111111")

    def test_coverage_accepts_success_and_unfinished_stock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current_list = root / "site_data" / "current_stock_list.json"
            current_list.parent.mkdir(parents=True)
            current_list.write_text(
                json.dumps(
                    {
                        "stocks": [
                            {"account": "账户甲", "type": "stock", "name": "测试股票甲", "code": "111111"},
                            {"account": "账户甲", "type": "stock", "name": "测试股票乙", "code": "222222"},
                            {"account": "账户乙", "type": "lof", "name": "测试基金", "code": "333333"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            reports_dir = root / "reports"
            reports_dir.mkdir()
            (reports_dir / "report_20990110.md").write_text(
                """# 2099-01-10 股票日报

## 分析结果摘要

- 测试股票甲(111111)：观望 | 评分 50 | 震荡

## 测试股票甲(111111)

完整分析。

## 未完成分析标的

- 测试股票乙(222222)：行情数据获取失败
""",
                encoding="utf-8",
            )

            passed, missing = coverage.check_coverage(
                current_list,
                root / "site_data" / "holdings_snapshot.json",
                reports_dir,
            )

            self.assertTrue(passed)
            self.assertEqual(missing, [])

    def test_report_validation_uses_latest_log_and_last_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports_dir = root / "reports"
            logs_dir = root / "logs"
            reports_dir.mkdir()
            logs_dir.mkdir()
            report = reports_dir / "report_20990110.md"
            report.write_text("# 正常日报\n" + ("有效正文。" * 200), encoding="utf-8")

            old_log = logs_dir / "stock_analysis_debug_20990109.log"
            latest_log = logs_dir / "stock_analysis_debug_20990110.log"
            old_log.write_text("成功: 0, 失败: 2\n", encoding="utf-8")
            latest_log.write_text(
                "成功: 0, 失败: 2\n中间日志\n成功：2，失败：0\n",
                encoding="utf-8",
            )
            os.utime(old_log, (1, 1))
            os.utime(latest_log, (2, 2))

            self.assertEqual(report_valid.validate_report(reports_dir, logs_dir), 0)

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
        self.assertEqual(provider.request_count, 1)
        self.assertLessEqual(manager.kwargs[0]["start_date"], "2026-03-20")

    def test_verified_advice_does_not_refetch_or_keep_stale_diagnostic(self) -> None:
        class UnexpectedProvider(advice.PriceProvider):
            def get_bars(self, code: str, analysis_date: date):
                raise AssertionError(f"unexpected price fetch for {code} on {analysis_date}")

        record = {
            "date": "2026-06-18",
            "code": "600961",
            "action": "观望",
            "sentiment": "震荡",
            "price_warning": "旧价格诊断",
            "d1_status": "已验证",
            "d1_close": 10.1,
            "d5_status": "已验证",
            "d5_close": 10.2,
            "d20_status": "已验证",
            "d20_close": 10.3,
        }

        result = advice.evaluate_record(record, UnexpectedProvider())

        self.assertNotIn("price_warning", result)
        self.assertTrue(all(result[f"{period}_status"] == "已验证" for period in advice.PERIODS))

    def test_large_history_fetches_once_per_unique_code(self) -> None:
        manager = _CountingManager()
        provider = advice.DataFetcherPriceProvider()
        provider._manager = manager
        codes = ["111111", "222222", "333333", "444444", "555555", "666666"]

        for index in range(68):
            provider.get_bars(codes[index % len(codes)], date(2026, 6, 15) + timedelta(days=index // 6))

        self.assertEqual(provider.request_count, len(codes))
        self.assertEqual(len(manager.calls), len(codes))

    def test_pages_keep_account_contract_and_pass_html_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports_dir = root / "reports"
            site_data_dir = root / "site_data"
            site_dir = root / "site"
            site_reports_dir = site_dir / "reports"
            site_accounts_dir = site_dir / "accounts"
            reports_dir.mkdir()
            site_data_dir.mkdir()
            site_dir.mkdir()

            snapshot = {
                "generated_at": "2099-01-10 18:00:00",
                "source_url": "test:fixture",
                "accounts": {
                    "账户甲": {
                        "stock": [
                            {"account": "账户甲", "type": "stock", "name": "测试股票甲", "code": "111111"},
                            {"account": "账户甲", "type": "stock", "name": "测试股票乙", "code": "222222"},
                        ],
                        "lof": [],
                        "otc": [],
                    },
                    "账户乙": {
                        "stock": [],
                        "lof": [
                            {"account": "账户乙", "type": "lof", "name": "科技主题LOF", "code": "333333"}
                        ],
                        "otc": [],
                    },
                    "账户丙": {
                        "stock": [],
                        "lof": [],
                        "otc": [
                            {"account": "账户丙", "type": "otc", "name": "全球主题基金", "code": "444444"}
                        ],
                    },
                },
                "type_labels": holdings.TYPE_LABELS,
            }
            snapshot_path = site_data_dir / "holdings_snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            (reports_dir / "report_20990110.md").write_text(
                """# 2099-01-10 股票日报

## 分析结果摘要

- 测试股票甲(111111)：观望 | 评分 50 | 震荡

## 测试股票甲(111111)

### 核心结论

公司基本面保持稳定。

### 作战计划

- 关注后续公开信息变化。

## 未完成分析标的

- 测试股票乙(222222)：Gemini 模型服务暂不可用

## LOF/ETF 组合复盘

### 账户乙

#### 持有标的

- 科技主题LOF(333333)

#### 组合观察

- 主题暴露偏科技成长，组合结构清晰。

#### 配置节奏

- 当前仅做账户配置观察。

#### 后续观察

- 关注对应指数和行业景气变化。

## 场外基金组合复盘

### 账户丙

#### 持有基金

- 全球主题基金(444444)

#### 组合观察

组合在
""",
                encoding="utf-8",
            )

            current_record = {
                "date": "2099-01-10",
                "code": "111111",
                "name": "测试股票甲",
                "account": "账户甲",
                "action": "观望",
                "score": 50,
                "sentiment": "震荡",
                "action_group": "持有/观望类",
                "is_current_holding_now": True,
                "d1_status": "等待验证",
                "d5_status": "等待验证",
                "d20_status": "等待验证",
            }
            accuracy = advice.build_accuracy_with_metadata(
                [current_record],
                latest_report_date="2099-01-10",
                latest_report_name="report_20990110.md",
                new_advice_count=1,
            )
            (site_dir / "advice_backtest.html").write_text(
                advice.render_html(accuracy),
                encoding="utf-8",
            )

            patches = (
                (pages, "ROOT_DIR", root),
                (pages, "REPORTS_DIR", reports_dir),
                (pages, "HOLDINGS_SNAPSHOT_PATH", snapshot_path),
                (pages, "SITE_DIR", site_dir),
                (pages, "SITE_REPORTS_DIR", site_reports_dir),
                (pages, "SITE_ACCOUNTS_DIR", site_accounts_dir),
                (html_check, "ROOT_DIR", root),
                (html_check, "SITE_DIR", site_dir),
                (html_check, "SITE_REPORTS_DIR", site_reports_dir),
                (html_check, "SITE_ACCOUNTS_DIR", site_accounts_dir),
            )
            with ExitStack() as stack:
                for module, name, value in patches:
                    stack.enter_context(patch.object(module, name, value))
                pages.build_pages()

                report_html = (site_reports_dir / "report_20990110.html").read_text(encoding="utf-8")
                self.assertEqual(report_html.count('class="summary-item"'), 4)
                self.assertEqual(report_html.count('class="holding-item"'), 4)
                self.assertIn("规则版组合兜底复盘", report_html)
                self.assertNotIn("A股个股（", report_html)
                self.assertNotIn("场内基金/ETF/LOF（", report_html)
                self.assertNotIn("场外基金（", report_html)
                self.assertIn("原始 AI 股票日报", report_html)
                self.assertEqual(len(list(site_accounts_dir.glob("*.html"))), 3)
                self.assertEqual(html_check.main(), 0)

    def test_workflow_has_runtime_headroom(self) -> None:
        workflow = (holdings.ROOT_DIR / ".github" / "workflows" / "00-daily-analysis.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("vars.ANALYSIS_TIMEOUT_MINUTES || '45'", workflow)
        self.assertIn("cron: '23 10 * * 1-5'", workflow)
        self.assertIn("cron: '23 12 * * 1-5'", workflow)

        ordered_steps = (
            "检查持仓日报链路回归",
            "生成持仓自选股列表",
            "执行股票分析",
            "检查有效股票日报",
            "检查日报 code 覆盖",
            "更新 AI 建议准确性回测",
            "生成静态报告网页",
            "检查静态报告网页",
            "Upload Pages artifact",
        )
        positions = [workflow.index(step) for step in ordered_steps]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
