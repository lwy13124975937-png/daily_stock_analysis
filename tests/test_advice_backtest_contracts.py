from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import update_advice_backtest as advice
from src.core.session_calendar import SessionCalendarUnavailable
from src.reports.contracts import (
    ADVICE_EVALUATION_VERSION,
    ActionCode,
    DataIntegrityError,
    SentimentCode,
    normalize_action,
    normalize_sentiment,
    read_jsonl_strict_bytes,
    write_json_atomic,
)
from src.reports.structured_stock_report import build_structured_stock_report


TZ = ZoneInfo("Asia/Shanghai")


class StaticSessionCalendar:
    def __init__(self, sessions: list[date]) -> None:
        self.sessions = sorted(sessions)

    def sessions_between(self, start: date, end: date) -> list[date]:
        return [item for item in self.sessions if start <= item <= end]

    def completed_session_at(self, moment: datetime) -> date:
        completed = [item for item in self.sessions if item <= moment.astimezone(TZ).date()]
        if not completed:
            raise SessionCalendarUnavailable("no completed fixture session")
        return completed[-1]


class TimedSessionCalendar(StaticSessionCalendar):
    def completed_session_at(self, moment: datetime) -> date:
        local = moment.astimezone(TZ)
        completed = [item for item in self.sessions if item < local.date()]
        if local.date() in self.sessions and local.hour >= 15:
            completed.append(local.date())
        if not completed:
            raise SessionCalendarUnavailable("no completed fixture session")
        return completed[-1]


def _structured_report(moment: datetime, calendar: StaticSessionCalendar, *, success: bool = True) -> dict:
    result = SimpleNamespace(
        code="600000",
        name="浦发银行",
        operation_advice="持有观察",
        trend_prediction="偏多",
        sentiment_score=61,
        analysis_summary="结构化摘要。",
        sector_position="银行",
        trend_analysis="趋势。",
        technical_analysis="技术。",
        fundamental_analysis="基本面。",
        risk_warning="风险。",
        dashboard={},
        get_core_conclusion=lambda: "核心结论。",
    )
    return build_structured_stock_report(
        results=[result] if success else [],
        failed_results=[] if success else [{"code": "600000", "reason": "provider unavailable"}],
        expected_stock_codes=["600000"],
        generated_at=moment,
        report_date=moment.astimezone(TZ).date(),
        run_id=f"run-{moment.strftime('%H%M')}",
        calendar=calendar,
    )


def _extract_report(root: Path, payload: dict) -> list[dict]:
    path = root / "report.json"
    write_json_atomic(path, payload)
    holdings = {
        "600000": advice.Holding("600000", "浦发银行", ("账户甲",), "stock")
    }
    return advice.extract_advice_from_structured_report(path, holdings)


class AdviceBacktestContractTests(unittest.TestCase):
    def test_intraday_run_uses_previous_session_and_creates_no_official_record(self) -> None:
        calendar = TimedSessionCalendar([date(2026, 8, 27), date(2026, 8, 28)])
        payload = _structured_report(datetime(2026, 8, 28, 10, tzinfo=TZ), calendar)
        self.assertEqual(payload["anchor_session"], "2026-08-27")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(_extract_report(Path(temp_dir), payload), [])

    def test_post_close_run_creates_the_unique_official_record(self) -> None:
        calendar = TimedSessionCalendar([date(2026, 8, 27), date(2026, 8, 28)])
        payload = _structured_report(datetime(2026, 8, 28, 16, tzinfo=TZ), calendar)
        with tempfile.TemporaryDirectory() as temp_dir:
            records = _extract_report(Path(temp_dir), payload)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["anchor_session"], "2026-08-28")
        self.assertEqual(records[0]["recommendation_id"], "official:2026-08-28:600000")

    def test_post_close_retry_is_an_idempotent_no_op(self) -> None:
        calendar = TimedSessionCalendar([date(2026, 8, 28)])
        with tempfile.TemporaryDirectory() as temp_dir:
            first = _extract_report(
                Path(temp_dir), _structured_report(datetime(2026, 8, 28, 16, tzinfo=TZ), calendar)
            )
            retry = _extract_report(
                Path(temp_dir), _structured_report(datetime(2026, 8, 28, 16, 30, tzinfo=TZ), calendar)
            )
        merged, stats = advice.merge_new_official_records(first, retry)
        self.assertEqual(len(merged), 1)
        self.assertEqual(stats, {"added": 0, "exact_retries_skipped": 1, "conflicting_retries_skipped": 0})
        self.assertEqual(merged[0]["run_id"], "run-1600")

    def test_failed_post_close_attempt_can_be_recovered_without_duplicate(self) -> None:
        calendar = TimedSessionCalendar([date(2026, 8, 28)])
        with tempfile.TemporaryDirectory() as temp_dir:
            failed = _extract_report(
                Path(temp_dir),
                _structured_report(datetime(2026, 8, 28, 16, tzinfo=TZ), calendar, success=False),
            )
            recovered = _extract_report(
                Path(temp_dir), _structured_report(datetime(2026, 8, 28, 16, 30, tzinfo=TZ), calendar)
            )
        merged, stats = advice.merge_new_official_records(failed, recovered)
        self.assertEqual(len(merged), 1)
        self.assertEqual(stats["added"], 1)

    def test_saturday_run_anchors_friday_without_weekend_recommendation(self) -> None:
        calendar = TimedSessionCalendar([date(2026, 8, 27), date(2026, 8, 28)])
        payload = _structured_report(datetime(2026, 8, 29, 16, tzinfo=TZ), calendar)
        self.assertEqual(payload["anchor_session"], "2026-08-28")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(_extract_report(Path(temp_dir), payload), [])

    def test_exchange_holiday_run_uses_last_formal_session(self) -> None:
        calendar = TimedSessionCalendar([date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 9)])
        payload = _structured_report(datetime(2026, 10, 5, 16, tzinfo=TZ), calendar)
        self.assertEqual(payload["anchor_session"], "2026-09-30")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(_extract_report(Path(temp_dir), payload), [])

    def test_action_and_sentiment_are_independent_and_negations_win(self) -> None:
        cases = {
            "持有": ActionCode.HOLD,
            "持有观察": ActionCode.HOLD_WATCH,
            "观望": ActionCode.OBSERVE,
            "等待": ActionCode.OBSERVE,
            "不建议买入": ActionCode.OBSERVE,
            "暂不卖出": ActionCode.HOLD,
            "不宜追涨": ActionCode.OBSERVE,
            "": ActionCode.UNKNOWN,
            "无法识别": ActionCode.UNKNOWN,
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_action(raw), expected)
        self.assertEqual(normalize_action("观望"), ActionCode.OBSERVE)
        self.assertEqual(normalize_sentiment("强烈看多"), SentimentCode.BULLISH)
        self.assertEqual(normalize_action("持有"), ActionCode.HOLD)
        self.assertEqual(normalize_sentiment("偏空"), SentimentCode.BEARISH)

    def test_holiday_sessions_control_maturity_not_weekdays(self) -> None:
        sessions = [
            date(2026, 9, 29), date(2026, 9, 30),
            date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13),
            date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16),
        ]
        calendar = StaticSessionCalendar(sessions)
        provider = advice.MockPriceProvider({
            "600000": [advice.DailyBar(date(2026, 9, 30), 10.0), advice.DailyBar(date(2026, 10, 9), 10.4)]
        })
        record = {
            "date": "2026-09-30", "anchor_session": "2026-09-30",
            "anchor_precision": "exact_session", "code": "600000", "type": "stock",
            "action_raw": "观望", "sentiment_raw": "看多",
        }
        waiting = advice.evaluate_record(
            record, provider, calendar=calendar,
            current_time=datetime(2026, 10, 8, 20, tzinfo=TZ),
        )
        self.assertEqual(waiting["d1_status"], "等待验证")
        verified = advice.evaluate_record(
            record, provider, calendar=calendar,
            current_time=datetime(2026, 10, 9, 20, tzinfo=TZ),
        )
        self.assertEqual(verified["d1_status"], "已验证")
        self.assertFalse(verified["d1_observe_consistent"])
        self.assertTrue(verified["d1_sentiment_aligned"])
        self.assertIsNone(verified["d1_direction_hit"])

    def test_non_session_legacy_date_anchors_to_previous_completed_session(self) -> None:
        calendar = StaticSessionCalendar([date(2026, 8, 28), date(2026, 8, 31)])
        provider = advice.MockPriceProvider({
            "600000": [advice.DailyBar(date(2026, 8, 28), 10), advice.DailyBar(date(2026, 8, 31), 9.8)]
        })
        result = advice.evaluate_record(
            {"date": "2026-08-30", "code": "600000", "action": "持有", "sentiment": "中性"},
            provider, calendar=calendar,
            current_time=datetime(2026, 8, 31, 20, tzinfo=TZ),
        )
        self.assertEqual(result["anchor_session"], "2026-08-28")
        self.assertEqual(result["anchor_precision"], "legacy_date_only")
        self.assertEqual(result["d1_status"], "已验证")

    def test_evaluation_version_recomputes_derived_fields_without_mutating_raw(self) -> None:
        calendar = StaticSessionCalendar([date(2026, 6, 18), date(2026, 6, 19)])
        raw = {
            "date": "2026-06-18", "anchor_session": "2026-06-18", "anchor_precision": "exact_session",
            "code": "600961", "action": "持有", "sentiment": "看空", "summary": "原始摘要",
            "advice_close": 10.0, "d1_date": "2026-06-19", "d1_close": 9.5,
            "d1_return": -0.05, "d1_status": "已验证", "d1_hit": True,
            "evaluation_version": "1",
        }
        result = advice.evaluate_record(
            raw, advice.MockErrorPriceProvider(), calendar=calendar,
            current_time=datetime(2026, 6, 19, 20, tzinfo=TZ),
        )
        self.assertEqual(result["action_raw"], "持有")
        self.assertEqual(result["sentiment_raw"], "看空")
        self.assertEqual(result["summary"], "原始摘要")
        self.assertEqual(result["summary_raw"], "原始摘要")
        self.assertEqual(result["evaluation_version"], ADVICE_EVALUATION_VERSION)
        self.assertNotIn("d1_hit", result)
        self.assertTrue(result["d1_hold_drawdown_flag"])
        self.assertTrue(result["d1_sentiment_aligned"])

    def test_same_day_exact_retry_is_idempotent_but_conflict_fails(self) -> None:
        first = {"date": "2026-08-29", "code": "600000", "type": "stock", "action": "观望"}
        self.assertEqual(len(advice.merge_history([first, dict(first)])), 1)
        with self.assertRaises(DataIntegrityError):
            advice.merge_history([first, {**first, "action": "买入"}])

    def test_corrupted_jsonl_and_unverified_history_fail_closed(self) -> None:
        with self.assertRaisesRegex(DataIntegrityError, r":2:"):
            read_jsonl_strict_bytes(b'{"ok":1}\n{broken}\n', source="fixture")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history = root / "history.jsonl"
            history.write_text('{"date":"2026-08-29","code":"600000"}\n', encoding="utf-8")
            with self.assertRaises(DataIntegrityError):
                advice._read_local_history_with_contract(history, root / "missing-manifest.json")

    def test_arbitrary_history_path_cannot_borrow_site_accuracy_witness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history = root / "untrusted.jsonl"
            history.write_text('{"date":"2026-08-29","code":"600000"}\n', encoding="utf-8")
            site_accuracy = root / "site-accuracy.json"
            site_accuracy.write_text(
                json.dumps({"summary_all_history": {"total_advice": 1}}),
                encoding="utf-8",
            )
            with patch.object(advice, "SITE_DATA_ACCURACY_PATH", site_accuracy):
                with self.assertRaises(DataIntegrityError):
                    advice._read_local_history_with_contract(history, root / "missing-manifest.json")

    def test_remote_failure_requires_explicit_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(advice, "LOCAL_HISTORY_PATH", root / "local.jsonl"),
                patch.object(advice, "LOCAL_HISTORY_MANIFEST_PATH", root / "local-manifest.json"),
                patch.object(advice, "SITE_DATA_HISTORY_PATH", root / "site.jsonl"),
                patch.object(advice, "SITE_DATA_HISTORY_MANIFEST_PATH", root / "site-manifest.json"),
                patch.object(advice, "fetch_pages_history", side_effect=DataIntegrityError("offline")),
            ):
                with self.assertRaises(DataIntegrityError):
                    advice.load_history()
                bootstrapped = advice.load_history(allow_bootstrap_empty_history=True)
                self.assertEqual(bootstrapped.status, "bootstrap_empty_explicit")
                self.assertEqual(bootstrapped.records, [])

    def test_metrics_keep_direction_hold_observe_and_sentiment_separate(self) -> None:
        records = [
            {"action_raw": "卖出", "sentiment_raw": "看空", "d1_status": "已验证", "d1_direction_hit": True, "d1_sentiment_aligned": True},
            {"action_raw": "持有", "sentiment_raw": "看多", "d1_status": "已验证", "d1_return": 0.12, "d1_hold_drawdown_flag": False, "d1_sentiment_aligned": True},
            {"action_raw": "观望", "sentiment_raw": "中性", "d1_status": "已验证", "d1_return": 0.01, "d1_observe_consistent": True, "d1_sentiment_aligned": True},
        ]
        accuracy = advice.build_accuracy(records)
        metrics = accuracy["metrics_all_history"]
        self.assertEqual(metrics["directional_action"]["total_advice"], 1)
        self.assertEqual(metrics["hold_results"]["total_advice"], 1)
        self.assertEqual(metrics["observe_consistency"]["total_advice"], 1)
        self.assertEqual(metrics["sentiment_alignment"]["total_advice"], 3)
        self.assertNotIn("hit_rate", accuracy.get("summary_all_history", {}))

    def test_report_date_never_uses_file_mtime(self) -> None:
        with self.assertRaises(DataIntegrityError):
            advice.report_date_text(Path("report-without-business-date.json"))

    def test_summary_truncates_only_at_a_complete_sentence(self) -> None:
        source = "第一句完整。" + "第二句也完整。" + ("长内容" * 300)
        compact = advice.compact_summary(source, limit=20)
        self.assertEqual(compact, "第一句完整。第二句也完整。（摘要已截取）")
        no_boundary = "连续文本" * 200
        self.assertEqual(advice.compact_summary(no_boundary, limit=20), no_boundary)


if __name__ == "__main__":
    unittest.main()
