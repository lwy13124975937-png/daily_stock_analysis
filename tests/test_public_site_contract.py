from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import update_advice_backtest as advice
from src.reports.contracts import (
    ADVICE_EVALUATION_VERSION,
    HOLDINGS_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    write_json_atomic,
)
from src.site.builder import build_site, source_fingerprint
from src.site.security import safe_public_url, sanitize_html
from src.site.validator import _check_page, validate_site


TZ = ZoneInfo("Asia/Shanghai")


def _write_fixture(root: Path) -> Path:
    reports = root / "reports"
    site_data = root / "site_data"
    reports.mkdir(parents=True)
    site_data.mkdir(parents=True)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": "run-fixture",
        "generated_at": "2026-08-28T18:30:00+08:00",
        "market_data_as_of": "2026-08-28",
        "anchor_session": "2026-08-28",
        "report_date": "2026-08-28",
        "report_type": "stock_daily",
        "markdown_file": "report_20260828.md",
        "expected_stock_codes": ["600000", "000001"],
        "expected_count": 2,
        "success_count": 1,
        "failure_count": 1,
        "success_ids": ["600000"],
        "failure_ids": ["000001"],
        "status": "degraded",
        "results": [
            {
                "code": "600000", "name": "浦发银行", "success": True, "failure_code": "none",
                "action_raw": "持有观察", "action_normalized": "hold_watch",
                "sentiment_raw": "偏多", "sentiment_normalized": "bullish", "score": 61,
                "public_summary": "基本面仍需持续核验。",
                "sections": {"core_conclusion": "低风险证据仍需持续核验。", "battle_plan": ["关注公开信息变化。"]},
            },
            {
                "code": "000001", "name": "平安银行", "success": False,
                "failure_code": "provider_unavailable", "public_message": "本标的模型服务暂不可用，未能完成。",
                "action_raw": "", "action_normalized": "unknown", "sentiment_raw": "",
                "sentiment_normalized": "unknown", "score": None, "public_summary": "", "sections": {},
            },
        ],
        "portfolio_reviews": [
            {
                "schema_version": 1, "account": "账户甲", "asset_type": "lof", "status": "rule_fallback",
                "themes": ["宽基指数"], "failure_code": "llm_truncated", "generated_by": "rules",
                "holdings": [{"name": "宽基ETF", "code": "510300"}],
                "sections": {"组合观察": ["AI 组合复盘未完成，以下为规则版组合兜底复盘。"], "配置节奏": ["仅做组合层面观察。"], "后续观察": ["关注集中度。"]},
            },
            {
                "schema_version": 1, "account": "账户乙", "asset_type": "otc", "status": "ai",
                "themes": ["海外资产"], "failure_code": "none", "generated_by": "llm",
                "holdings": [{"name": "全球基金", "code": "012345"}],
                "sections": {"组合观察": ["组合覆盖海外资产与宽基方向。"], "风格暴露": ["风格较分散。"], "配置节奏": ["仅做组合层面观察。"], "后续观察": ["关注主题重叠。"]},
            },
        ],
    }
    write_json_atomic(reports / "report_20260828.json", report)
    (reports / "report_20260828.md").write_text(
        "# 原始 AI 股票日报\n\n禁止加仓摊薄成本。\n\n"
        "<script>alert(1)</script><iframe src=\"https://evil.example\"></iframe>\n"
        "<img src=x onerror=\"alert(2)\"><a href=\"JaVaScRiPt:alert(3)\">危险链接</a>\n",
        encoding="utf-8",
    )
    snapshot = {
        "schema_version": HOLDINGS_SCHEMA_VERSION,
        "generated_at": "2026-08-28T18:00:00+08:00",
        "source_kind": "environment_secret", "source_label": "protected holdings input",
        "source_fingerprint": "abc123", "source_link": None,
        "accounts": {
            "账户甲": {
                "stock": [
                    {"account": "账户甲", "type": "stock", "name": "浦发银行", "code": "600000"},
                    {"account": "账户甲", "type": "stock", "name": "平安银行", "code": "000001"},
                ],
                "lof": [{"account": "账户甲", "type": "lof", "name": "宽基ETF", "code": "510300"}],
                "otc": [],
            },
            "账户乙": {
                "stock": [], "lof": [],
                "otc": [{"account": "账户乙", "type": "otc", "name": "全球基金", "code": "012345"}],
            },
        },
        "type_labels": {"stock": "A股个股", "lof": "场内基金/ETF/LOF", "otc": "场外基金"},
        "validation_warnings": [],
    }
    write_json_atomic(site_data / "holdings_snapshot.json", snapshot)
    write_json_atomic(
        site_data / "current_stock_list.json",
        {
            "schema_version": HOLDINGS_SCHEMA_VERSION,
            "generated_at": "2026-08-28T18:00:00+08:00",
            "source_kind": "fixture",
            "source_fingerprint": "fixture-stock-list",
            "stocks": [
                {"code": "600000", "name": "浦发银行", "type": "stock"},
                {"code": "000001", "name": "平安银行", "type": "stock"},
            ],
        },
    )

    record = {
        "schema_version": 2, "evaluation_version": ADVICE_EVALUATION_VERSION,
        "recommendation_id": "run-fixture:600000", "run_id": "run-fixture", "revision": 1, "official": True,
        "date": "2026-08-28", "report_date": "2026-08-28", "anchor_session": "2026-08-28",
        "anchor_precision": "exact_session", "generated_at": "2026-08-28T18:30:00+08:00",
        "market_data_as_of": "2026-08-28", "code": "600000", "name": "浦发银行", "type": "stock",
        "accounts": ["账户甲"], "action_raw": "持有观察", "action_normalized": "hold_watch",
        "sentiment_raw": "偏多", "sentiment_normalized": "bullish", "score": 61,
        "summary": "结构化摘要。", "is_current_holding_now": True, "is_current_holding_when_advised": True,
        "advice_close": 10.0, "advice_close_date": "2026-08-28",
        "d1_status": "等待验证", "d5_status": "等待验证", "d20_status": "等待验证",
    }
    advice.write_jsonl(site_data / "advice_history.jsonl", [record])
    write_json_atomic(site_data / "advice_history_manifest.json", advice.build_history_manifest([record]))
    accuracy = advice.build_accuracy_with_metadata(
        [record], latest_report_date="2026-08-28", latest_report_name="report_20260828.json", new_advice_count=1
    )
    write_json_atomic(site_data / "advice_accuracy.json", accuracy)
    steady = {
        "schema_version": 5, "model_version": "steady-income-risk-v5",
        "ruleset_version": "4.0.0", "evaluator_version": "5.0.0",
        "sector_model_version": "1.0.0", "evidence_version": "4.0.0",
        "price_model_version": "2.0.0",
        "generated_at": "2026-08-28T18:40:00+08:00", "as_of": "2026-08-28", "mode": "live",
        "data_status": "valid_zero", "source": "fixture",
        "selection_mode": "exhaustive", "universe_count": 5200, "prefilter_count": 0,
        "deep_budget": 0, "deep_evaluated_count": 0, "unevaluated_count": 0,
        "is_exhaustive": True,
        "universe": {"market": "沪深A股", "count": 5200, "source": "fixture", "complete": True},
        "screening_stats": {
            "universe_count": 5200, "known_plan_count": 0, "prefilter_eligible_count": 0,
            "deep_selected_count": 0, "deep_evaluated_count": 0, "qualified_count": 0,
            "deep_requested_count": 0, "completed_evaluation_count": 0, "success_count": 0,
            "data_insufficient_count": 0, "rejected_by_reason": {"plan_yield_outside_prefilter": 5200},
            "selection_mode": "exhaustive", "deep_budget": 0,
            "unevaluated_count": 0, "is_exhaustive": True,
            "terminal_status_distribution": {
                "evaluated_qualified": 0, "evaluated_rejected": 0, "insufficient_evidence": 0,
                "unsupported_sector_model": 0, "provider_failure": 0, "internal_error": 0,
            },
        },
        "evaluated_count": 0, "qualified_count": 0, "candidates": [], "excluded": [],
        "methodology": {"priority": "风险硬门槛优先。", "replay": "历史复权价格回放。"},
    }
    write_json_atomic(site_data / "steady_income.json", steady)
    return root / "site"


class PublicSiteContractTests(unittest.TestCase):
    def test_frontend_has_no_provider_derived_raw_html_sink(self) -> None:
        frontend = Path(__file__).resolve().parents[1] / "apps" / "dsa-web" / "src"
        hits: list[tuple[str, str]] = []
        for path in frontend.rglob("*.tsx"):
            text = path.read_text(encoding="utf-8")
            for token in ("dangerouslySetInnerHTML", ".innerHTML", "insertAdjacentHTML", "srcDoc="):
                if token in text:
                    hits.append((path.relative_to(frontend).as_posix(), token))
        self.assertEqual(hits, [("pages/LoginPage.tsx", "dangerouslySetInnerHTML")])
        login = (frontend / "pages" / "LoginPage.tsx").read_text(encoding="utf-8")
        self.assertIn("<style dangerouslySetInnerHTML={{ __html: `", login)

    def test_sanitizer_blocks_active_content_and_unsafe_urls(self) -> None:
        dirty = '<ScRiPt>alert(1)</ScRiPt><iframe src="x"></iframe><img src=x onerror=alert(1)><a href="JaVaScRiPt:alert(2)" onclick="x()">x</a><p style="background:url(x)">ok</p>'
        clean = sanitize_html(dirty).lower()
        for token in ("<script", "<iframe", "<img", "onerror", "onclick", "javascript:", "style="):
            self.assertNotIn(token, clean)
        self.assertIn("<p>ok</p>", clean)
        self.assertIsNone(safe_public_url("javascript:alert(1)"))
        self.assertIsNone(safe_public_url("file:///tmp/private"))

    def test_staging_build_is_content_addressed_manifested_and_removes_ghost_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site = _write_fixture(root)
            site.mkdir()
            (site / "stale-account.html").write_text("stale", encoding="utf-8")
            generated_at = datetime(2026, 8, 28, 19, tzinfo=TZ)
            files = build_site(root=root, generated_at=generated_at)
            self.assertTrue(files)
            self.assertFalse((site / "stale-account.html").exists())
            self.assertEqual(validate_site(site), [])
            self.assertTrue((site / "assets" / "site.css").exists())
            html = (site / "reports" / "report_20260828.html").read_text(encoding="utf-8")
            self.assertIn("原始 AI 股票日报（审计留档）", html)
            self.assertIn('<details class="stock-row">', html)
            self.assertEqual(html.count("以下标的统一纳入账户级 LOF/ETF"), 1)
            self.assertNotIn("<script", html.lower())
            self.assertNotIn("<iframe", html.lower())
            self.assertNotIn("onerror", html.lower())
            self.assertNotIn("javascript:", html.lower())
            self.assertNotIn("<style", html.lower())
            advice_html = (site / "advice_backtest.html").read_text(encoding="utf-8")
            self.assertIn("方向性动作命中率", advice_html)
            self.assertIn("查看完整历史（1 条）", advice_html)
            steady_html = (site / "steady_income.html").read_text(encoding="utf-8")
            self.assertIn("个候选中没有满足全部硬条件的标的", steady_html)
            self.assertNotIn("股息率对应价格", steady_html)

            first_manifest = json.loads((site / "data" / "build_manifest.json").read_text(encoding="utf-8"))
            first_hashes = {
                path.relative_to(site).as_posix(): path.read_bytes()
                for path in site.rglob("*")
                if path.is_file() and path.relative_to(site).as_posix() != "data/build_manifest.json"
            }
            build_site(root=root, generated_at=datetime(2026, 8, 28, 19, 1, tzinfo=TZ))
            second_manifest = json.loads((site / "data" / "build_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(second_manifest["content_id"], first_manifest["content_id"])
            self.assertEqual(second_manifest["build_id"], first_manifest["build_id"])
            self.assertEqual(second_manifest["source_fingerprint"], first_manifest["source_fingerprint"])
            self.assertNotEqual(second_manifest["built_at"], first_manifest["built_at"])
            self.assertEqual(
                {
                    path.relative_to(site).as_posix(): path.read_bytes()
                    for path in site.rglob("*")
                    if path.is_file() and path.relative_to(site).as_posix() != "data/build_manifest.json"
                },
                first_hashes,
            )

    def test_source_fingerprint_tracks_participating_dirty_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "src" / "site" / "fixture_renderer.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            first = source_fingerprint(root)
            source.write_text("VALUE = 2\n", encoding="utf-8")
            second = source_fingerprint(root)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            self.assertNotEqual(first, second)
            self.assertEqual(source_fingerprint(root), first)

    def test_input_change_changes_content_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site = _write_fixture(root)
            build_site(root=root, generated_at=datetime(2026, 8, 28, 19, tzinfo=TZ))
            first = json.loads((site / "data" / "build_manifest.json").read_text(encoding="utf-8"))
            snapshot_path = root / "site_data" / "holdings_snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["source_fingerprint"] = "changed-input"
            write_json_atomic(snapshot_path, snapshot)
            build_site(root=root, generated_at=datetime(2026, 8, 28, 19, 1, tzinfo=TZ))
            second = json.loads((site / "data" / "build_manifest.json").read_text(encoding="utf-8"))
            self.assertNotEqual(first["content_id"], second["content_id"])
            self.assertNotEqual(first["build_id"], second["build_id"])

    def test_participating_source_change_changes_content_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site = _write_fixture(root)
            source = root / "src" / "site" / "fixture_renderer.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            build_site(root=root, generated_at=datetime(2026, 8, 28, 19, tzinfo=TZ))
            first = json.loads((site / "data" / "build_manifest.json").read_text(encoding="utf-8"))
            source.write_text("VALUE = 2\n", encoding="utf-8")
            build_site(root=root, generated_at=datetime(2026, 8, 28, 19, 1, tzinfo=TZ))
            second = json.loads((site / "data" / "build_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(first["git_sha"], second["git_sha"])
            self.assertNotEqual(first["source_fingerprint"], second["source_fingerprint"])
            self.assertNotEqual(first["content_id"], second["content_id"])
            self.assertNotEqual(first["build_id"], second["build_id"])

    def test_staging_failures_preserve_last_known_good_site(self) -> None:
        failures = (
            ("sanitizer", "src.site.builder.sanitize_html", RuntimeError("sanitizer failure")),
            ("manifest", "src.site.builder._validate_staging", ValueError("manifest validation failure")),
        )
        for label, target, error in failures:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                site = _write_fixture(root)
                site.mkdir()
                sentinel = site / "last-known-good.txt"
                sentinel.write_text("keep", encoding="utf-8")
                with patch(target, side_effect=error):
                    with self.assertRaises(type(error)):
                        build_site(root=root, generated_at=datetime(2026, 8, 28, 19, tzinfo=TZ))
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

        for label in ("broken-link", "hash-mismatch"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                site = _write_fixture(root)
                site.mkdir()
                sentinel = site / "last-known-good.txt"
                sentinel.write_text("keep", encoding="utf-8")
                if label == "broken-link":
                    module = __import__("src.site.builder", fromlist=["_render_index"])
                    original_render_index = module._render_index

                    def render_broken_index(*args, **kwargs):
                        html = original_render_index(*args, **kwargs)
                        return html.replace("</body>", '<a href="missing.html">x</a></body>', 1)

                    context = patch("src.site.builder._render_index", side_effect=render_broken_index)
                else:
                    original = __import__("src.site.builder", fromlist=["_manifest_file_entries"])._manifest_file_entries

                    def corrupt_entries(staging: Path):
                        entries = original(staging)
                        entries[0]["sha256"] = "0" * 64
                        return entries

                    context = patch("src.site.builder._manifest_file_entries", side_effect=corrupt_entries)
                with context:
                    with self.assertRaises(ValueError):
                        build_site(root=root, generated_at=datetime(2026, 8, 28, 19, tzinfo=TZ))
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_manifest_hash_and_broken_internal_link_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site = _write_fixture(root)
            build_site(root=root, generated_at=datetime(2026, 8, 28, 19, tzinfo=TZ))
            index = site / "index.html"
            original = index.read_text(encoding="utf-8")
            build_id = json.loads((site / "data" / "build_manifest.json").read_text(encoding="utf-8"))["build_id"]
            index.write_text(original.replace("</header>", '</header><a href="missing.html">missing</a>', 1), encoding="utf-8")
            errors = validate_site(site)
            self.assertTrue(any("manifest hash mismatch" in item for item in errors))
            self.assertTrue(any("broken internal link" in item for item in _check_page(site, index, build_id)))

    def test_invalid_utf8_and_cross_page_build_id_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site = _write_fixture(root)
            build_site(root=root, generated_at=datetime(2026, 8, 28, 19, tzinfo=TZ))
            account = next((site / "accounts").glob("*.html"))
            build_id = json.loads(
                (site / "data" / "build_manifest.json").read_text(encoding="utf-8")
            )["build_id"]
            account.write_text(
                account.read_text(encoding="utf-8").replace(build_id, "content-other", 1),
                encoding="utf-8",
            )
            errors = validate_site(site)
            self.assertTrue(any("manifest hash mismatch" in item for item in errors))
            self.assertTrue(any("build_id mismatch" in item for item in errors))

            index = site / "index.html"
            index.write_bytes(b"\xff\xfe\x00")
            errors = validate_site(site)
            self.assertTrue(any("invalid UTF-8 public file" in item for item in errors))

    def test_missing_fund_review_preserves_last_known_good_site(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site = _write_fixture(root)
            site.mkdir()
            sentinel = site / "last-known-good.txt"
            sentinel.write_text("keep", encoding="utf-8")
            report_path = root / "reports" / "report_20260828.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["portfolio_reviews"] = []
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "portfolio review coverage"):
                build_site(
                    root=root,
                    generated_at=datetime(2026, 8, 28, 19, tzinfo=TZ),
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
