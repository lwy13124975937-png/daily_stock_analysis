#!/usr/bin/env python3
"""Update AI advice backtest history and static Pages output.

This module is intentionally independent from the stock-analysis pipeline. It
does not call Gemini, OpenAI, DeepSeek, or any LLM. It only reads generated
reports, the public holdings snapshot, prior advice history, and market closes.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.session_calendar import (
    ExchangeSessionCalendar,
    SessionCalendar,
    SessionCalendarUnavailable,
    nth_session_after,
)
from src.reports.contracts import (
    ADVICE_EVALUATION_VERSION,
    ADVICE_HISTORY_SCHEMA_VERSION,
    ActionCode,
    DataIntegrityError,
    FailureCode,
    SentimentCode,
    normalize_action,
    normalize_sentiment,
    public_advice_record,
    read_json_strict,
    read_jsonl_strict,
    read_jsonl_strict_bytes,
    sha256_bytes,
    write_json_atomic,
    write_jsonl_atomic,
)


REPORTS_DIR = ROOT_DIR / "reports"
DATA_DIR = ROOT_DIR / "data"
SITE_DATA_DIR = ROOT_DIR / "site_data"
SITE_DATA_HISTORY_PATH = SITE_DATA_DIR / "advice_history.jsonl"
SITE_DATA_ACCURACY_PATH = SITE_DATA_DIR / "advice_accuracy.json"
SITE_DATA_HISTORY_MANIFEST_PATH = SITE_DATA_DIR / "advice_history_manifest.json"
LOCAL_HISTORY_PATH = DATA_DIR / "advice_history.jsonl"
LOCAL_ACCURACY_PATH = DATA_DIR / "advice_accuracy.json"
LOCAL_HISTORY_MANIFEST_PATH = DATA_DIR / "advice_history_manifest.json"
SNAPSHOT_PATH = ROOT_DIR / "site_data" / "holdings_snapshot.json"
PAGES_HISTORY_URL = (
    "https://lwy13124975937-png.github.io/"
    "daily_stock_analysis/data/advice_history.jsonl"
)
PAGES_HISTORY_MANIFEST_URL = (
    "https://lwy13124975937-png.github.io/"
    "daily_stock_analysis/data/advice_history_manifest.json"
)
PAGES_ACCURACY_URL = (
    "https://lwy13124975937-png.github.io/"
    "daily_stock_analysis/data/advice_accuracy.json"
)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = ADVICE_HISTORY_SCHEMA_VERSION
EVALUATION_VERSION = ADVICE_EVALUATION_VERSION
PERIODS = {"d1": 1, "d5": 5, "d20": 20}
HOLD_BAND = {"d1": 0.03, "d5": 0.05, "d20": 0.08}
REPORTABLE_TYPE = "stock"
DISCLAIMER = "本页面用于回看 AI 历史建议与后续真实行情的一致性，仅用于复盘模型表现，不构成投资建议。"
SENSITIVE_KEYS = {
    "unit_cost",
    "shares",
    "cost",
    "market_value",
    "profit",
    "amount",
    "total",
    "成本",
    "份额",
    "金额",
    "市值",
    "盈亏",
}


@dataclass(frozen=True)
class Holding:
    code: str
    name: str
    accounts: tuple[str, ...]
    type: str

    @property
    def account(self) -> str:
        """Legacy display alias; stock advice itself is not account-specific."""

        return " / ".join(self.accounts)


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    close: float


class PriceProvider:
    """Small adapter interface for close-price lookup."""

    def get_bars(self, code: str, analysis_date: date) -> tuple[list[DailyBar], str | None]:
        raise NotImplementedError


class DataFetcherPriceProvider(PriceProvider):
    """Use the repository's existing data provider manager when available."""

    def __init__(self, days: int = 90):
        self.days = days
        self._manager = None
        self._cache: dict[str, tuple[date, list[DailyBar], str | None]] = {}
        self.request_count = 0

    def _manager_instance(self):
        if self._manager is None:
            if str(ROOT_DIR) not in sys.path:
                sys.path.insert(0, str(ROOT_DIR))
            from data_provider import DataFetcherManager

            self._manager = DataFetcherManager()
        return self._manager

    @staticmethod
    def _canonical_code(code: str) -> str:
        raw = str(code or "").strip().upper()
        prefix_match = re.fullmatch(r"(?:SH|SZ|BJ)\.?([0-9]{6})", raw)
        if prefix_match:
            return prefix_match.group(1)
        suffix_match = re.fullmatch(r"([0-9]{6})\.(?:SH|SZ|BJ)", raw)
        if suffix_match:
            return suffix_match.group(1)
        return normalize_code(raw)

    @staticmethod
    def _code_variants(code: str) -> list[str]:
        raw = DataFetcherPriceProvider._canonical_code(code)
        variants = [raw]
        if raw.isdigit() and len(raw) == 6:
            if raw.startswith(("4", "8", "920")):
                market = "BJ"
            else:
                market = "SH" if raw.startswith(("5", "6", "9")) else "SZ"
            lower_market = market.lower()
            variants.extend(
                [
                    f"{market}{raw}",
                    f"{lower_market}{raw}",
                    f"{raw}.{market}",
                    f"{lower_market}.{raw}",
                ]
            )
        return list(dict.fromkeys(variants))

    def get_bars(self, code: str, analysis_date: date) -> tuple[list[DailyBar], str | None]:
        normalized = self._canonical_code(code)
        anchor_lookback_days = max(self.days, 30)
        requested_start = analysis_date - timedelta(days=anchor_lookback_days)
        cached = self._cache.get(normalized)
        if cached is not None and cached[0] <= requested_start:
            return list(cached[1]), cached[2]

        today = datetime.now(SHANGHAI_TZ).date()
        days = max(self.days, (today - requested_start).days + 30)
        error: str | None = None
        bars: list[DailyBar] = []
        try:
            # DataFetcherManager normalizes SH/SZ prefixes and suffixes itself.
            # Calling it once avoids repeating the entire provider failover chain
            # for equivalent spellings of the same A-share code.
            self.request_count += 1
            df, _source = self._manager_instance().get_daily_data(
                normalized,
                start_date=requested_start.strftime("%Y-%m-%d"),
                end_date=today.strftime("%Y-%m-%d"),
                days=days,
            )
            bars = dataframe_to_bars(df)
            if not bars:
                error = f"{normalized}: empty daily data"
        except Exception as exc:
            error = f"{normalized}: {type(exc).__name__}: {exc}"

        self._cache[normalized] = (requested_start, list(bars), error)
        return list(bars), error


class MockPriceProvider(PriceProvider):
    def __init__(self, bars_by_code: dict[str, list[DailyBar]]):
        self.bars_by_code = bars_by_code

    def get_bars(self, code: str, analysis_date: date) -> tuple[list[DailyBar], str | None]:
        return list(self.bars_by_code.get(code, [])), None


class MockErrorPriceProvider(PriceProvider):
    def __init__(self, bars_by_code: dict[str, list[DailyBar]] | None = None, error: str = "mock data missing"):
        self.bars_by_code = bars_by_code or {}
        self.error = error

    def get_bars(self, code: str, analysis_date: date) -> tuple[list[DailyBar], str | None]:
        return list(self.bars_by_code.get(code, [])), self.error


class StaticSessionCalendar:
    """Deterministic calendar used only by local self-tests and fixtures."""

    def __init__(self, sessions: Iterable[date]):
        self.sessions = sorted(set(sessions))

    def sessions_between(self, start: date, end: date) -> list[date]:
        return [session for session in self.sessions if start <= session <= end]

    def completed_session_at(self, moment: datetime) -> date:
        available = [session for session in self.sessions if session <= moment.astimezone(SHANGHAI_TZ).date()]
        if not available:
            raise SessionCalendarUnavailable("static calendar has no completed session")
        return available[-1]


@dataclass(frozen=True)
class HistoryLoadResult:
    records: list[dict[str, Any]]
    status: str
    source: str
    manifest: dict[str, Any]


def now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def now_text() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def report_date_text(report_path: Path) -> str:
    match = re.search(r"(20\d{6})", report_path.stem)
    if not match:
        raise DataIntegrityError(
            f"report filename has no explicit YYYYMMDD business date: {report_path}"
        )
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def report_date_key(report_path: Path) -> str:
    return report_date_text(report_path).replace("-", "")


def normalize_code(value: Any) -> str:
    code = str(value or "").strip()
    if code.isdigit() and 0 < len(code) <= 6:
        return code.zfill(6)
    return code


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_summary(value: Any, *, limit: int = 500) -> str:
    """Bound public summaries without cutting a sentence or Markdown token."""

    text = clean_text(value)
    if len(text) <= limit:
        return text
    candidates = [
        match.end()
        for match in re.finditer(r"[。！？；.!?;](?:[\"'”’）》】])?", text[: limit + 1])
    ]
    if candidates:
        return text[: candidates[-1]].rstrip() + "（摘要已截取）"
    # A source with no trustworthy boundary is kept intact.  A half sentence
    # is more misleading than a slightly longer public summary.
    return text


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict:
    if not path.exists():
        raise DataIntegrityError(f"holdings snapshot not found: {path}")
    payload = read_json_strict(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), dict):
        raise DataIntegrityError(f"invalid holdings snapshot schema: {path}")
    return payload


def current_stock_holdings(snapshot: dict) -> dict[str, Holding]:
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(accounts, dict):
        return {}

    aggregate: dict[str, dict[str, Any]] = {}
    for account, groups in accounts.items():
        if not isinstance(groups, dict):
            continue
        items = groups.get(REPORTABLE_TYPE, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            code = normalize_code(item.get("code"))
            if not code:
                continue
            entry = aggregate.setdefault(
                code,
                {"name": clean_text(item.get("name")) or code, "accounts": set()},
            )
            account_name = clean_text(item.get("account")) or clean_text(account)
            if account_name:
                entry["accounts"].add(account_name)
    return {
        code: Holding(
            code=code,
            name=str(entry["name"]),
            accounts=tuple(sorted(entry["accounts"])),
            type=REPORTABLE_TYPE,
        )
        for code, entry in aggregate.items()
    }


def load_current_stock_holdings() -> dict[str, Holding]:
    snapshot = load_snapshot()
    holdings = current_stock_holdings(snapshot)
    if not holdings:
        raise DataIntegrityError("holdings snapshot contains no enabled stock holdings")
    return holdings


def latest_stock_report(reports_dir: Path = REPORTS_DIR) -> Path | None:
    reports = [p for p in reports_dir.glob("report_20*.md") if p.is_file()]
    if not reports:
        return None
    return max(reports, key=lambda path: (report_date_key(path), path.name))


def latest_structured_stock_report(reports_dir: Path = REPORTS_DIR) -> Path | None:
    reports = [p for p in reports_dir.glob("report_20*.json") if p.is_file()]
    if not reports:
        return None
    return max(reports, key=lambda path: (report_date_key(path), path.name))


def _split_section(markdown_text: str, heading_keyword: str) -> str:
    heading_re = re.compile(r"^##+\s+(.+?)\s*$", re.MULTILINE)
    for match in heading_re.finditer(markdown_text):
        if heading_keyword not in clean_text(match.group(1)):
            continue
        start = match.end()
        next_heading = heading_re.search(markdown_text, start)
        end = next_heading.start() if next_heading else len(markdown_text)
        return markdown_text[start:end]
    return ""


ADVICE_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:[○●🔴🟢🟡]\s*)?"
    r"(?P<name>.+?)[（(]\s*(?P<code>\d{6})\s*[）)]"
    r"(?:\s*[（(]\s*(?P=code)\s*[）)])?"
    r"\s*(?:A股个股|股票|stock)?\s*[：:]\s*(?P<rest>.+?)\s*$",
    re.IGNORECASE,
)


def parse_advice_line(line: str) -> tuple[str, str, str, int | None, str, str] | None:
    text = clean_text(line).replace("｜", "|")
    match = ADVICE_RE.match(text)
    if not match:
        return None

    code = normalize_code(match.group("code"))
    name = clean_text(match.group("name")).lstrip("-+* ")
    rest = clean_text(match.group("rest")).replace("｜", "|")
    if is_failed_advice_text(rest):
        return None
    score_match = re.search(r"评分\s*[:：]?\s*(-?\d+)", rest)
    score = int(score_match.group(1)) if score_match else None

    parts = [clean_text(part) for part in rest.split("|") if clean_text(part)]
    action = "unknown"
    sentiment = "unknown"
    if parts:
        action = re.sub(r"评分\s*[:：]?\s*-?\d+", "", parts[0]).strip(" ：:|") or "unknown"
    for part in reversed(parts):
        if "评分" not in part:
            sentiment = part or "unknown"
            break

    summary = f"{name}({code})：{rest}" if rest else f"{name}({code})"
    return code, name, action, score, sentiment, summary


def is_failed_advice_text(text: str) -> bool:
    compact = clean_text(text).lower()
    return any(
        token in compact
        for token in (
            "分析失败",
            "未完成分析",
            "本标的未完成",
            "失败原因",
            "gemini api",
            "模型服务暂不可用",
            "额度超限",
        )
    )


def extract_advice_from_report(report_path: Path, holdings: dict[str, Holding]) -> list[dict[str, Any]]:
    if report_path is None or not report_path.exists():
        return []
    try:
        markdown_text = report_path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataIntegrityError(f"invalid UTF-8 legacy report: {report_path}: {exc}") from exc
    summary_section = _split_section(markdown_text, "分析结果摘要")
    if not summary_section:
        summary_section = markdown_text

    report_date = report_date_text(report_path)
    snapshot_date = report_date
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for line in summary_section.splitlines():
        parsed = parse_advice_line(line)
        if not parsed:
            continue
        code, report_name, action, score, sentiment, summary = parsed
        holding = holdings.get(code)
        if holding is None:
            continue
        key = (report_date, code)
        records[key] = {
            "schema_version": SCHEMA_VERSION,
            "date": report_date,
            "code": code,
            "name": holding.name or report_name or code,
            "type": REPORTABLE_TYPE,
            "accounts": list(holding.accounts),
            "action_raw": action,
            "action_normalized": normalize_action(action).value,
            "score": score,
            "sentiment_raw": sentiment,
            "sentiment_normalized": normalize_sentiment(sentiment).value,
            "summary": summary[:240],
            "source_report": f"reports/{report_path.name}",
            "holding_snapshot_date": snapshot_date,
            "is_current_holding_when_advised": True,
            "advice_close": None,
            "created_at": now_iso(),
            "anchor_session": report_date,
            "anchor_precision": "legacy_date_only",
            "anchor_assumption": "旧 Markdown 日报无精确生成时刻；按日报日期做兼容锚定。",
            "report_date": report_date,
            "run_id": "legacy_markdown",
            "recommendation_id": f"legacy:{report_date}:{code}",
            "revision": 1,
            "official": True,
        }

    if not records:
        print(f"WARNING: no stock advice extracted from {report_path}", file=sys.stderr)
    return list(records.values())


def extract_advice_from_structured_report(
    report_path: Path,
    holdings: dict[str, Holding],
) -> list[dict[str, Any]]:
    payload = read_json_strict(report_path)
    if not isinstance(payload, dict):
        raise DataIntegrityError(f"structured report must be an object: {report_path}")
    required = (
        "schema_version",
        "run_id",
        "generated_at",
        "market_data_as_of",
        "anchor_session",
        "report_date",
        "results",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise DataIntegrityError(f"structured report missing fields {missing}: {report_path}")
    anchor_session = parse_date(payload.get("anchor_session"))
    report_date = parse_date(payload.get("report_date"))
    generated_at_text = clean_text(payload.get("generated_at"))
    if anchor_session is None or report_date is None:
        raise DataIntegrityError(f"structured report has invalid report/anchor date: {report_path}")
    try:
        generated_at = datetime.fromisoformat(generated_at_text)
    except ValueError as exc:
        raise DataIntegrityError(f"structured report has invalid generated_at: {report_path}") from exc
    if generated_at.tzinfo is None:
        raise DataIntegrityError(f"structured report generated_at lacks timezone: {report_path}")

    # Official history is post-close only. Intraday/weekend runs may still
    # produce reports, but they do not create a new recommendation event.
    if report_date != anchor_session:
        print(
            "WARNING: structured report is not a same-session post-close recommendation; "
            f"report_date={report_date} anchor_session={anchor_session}. No advice appended.",
            file=sys.stderr,
        )
        return []

    records: list[dict[str, Any]] = []
    for item in payload.get("results", []):
        if not isinstance(item, dict) or item.get("success") is not True:
            continue
        code = normalize_code(item.get("code"))
        holding = holdings.get(code)
        if holding is None:
            continue
        action_raw = str(item.get("action_raw") or "")
        sentiment_raw = str(item.get("sentiment_raw") or "")
        records.append(
            public_advice_record(
                {
                    "schema_version": SCHEMA_VERSION,
                    "evaluation_version": EVALUATION_VERSION,
                    "recommendation_id": f"official:{anchor_session.isoformat()}:{code}",
                    "run_id": clean_text(payload.get("run_id")),
                    "revision": 1,
                    "official": True,
                    "date": anchor_session.isoformat(),
                    "report_date": report_date.isoformat(),
                    "anchor_session": anchor_session.isoformat(),
                    "anchor_precision": "exact_session",
                    "generated_at": generated_at.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds"),
                    "market_data_as_of": clean_text(payload.get("market_data_as_of")),
                    "code": code,
                    "name": holding.name or clean_text(item.get("name")) or code,
                    "type": REPORTABLE_TYPE,
                    "accounts": list(holding.accounts),
                    "action_raw": action_raw,
                    "action_normalized": normalize_action(action_raw).value,
                    "sentiment_raw": sentiment_raw,
                    "sentiment_normalized": normalize_sentiment(sentiment_raw).value,
                    "score": item.get("score"),
                    "summary_raw": str(item.get("public_summary") or ""),
                    "summary": compact_summary(item.get("public_summary")),
                    "source_report": f"reports/{report_path.name}",
                    "holding_snapshot_date": report_date.isoformat(),
                    "is_current_holding_when_advised": True,
                    "advice_close": None,
                    "created_at": generated_at.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds"),
                }
            )
        )
    return records


def read_history_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return read_jsonl_strict(path)


def _history_bytes(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(public_advice_record(record), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for record in records
    )


def build_history_manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    payload = _history_bytes(records)
    last = records[-1] if records else {}
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "count": len(records),
        "sha256": sha256_bytes(payload),
        "last_recommendation_id": last.get("recommendation_id"),
        "last_date": last.get("date"),
        "evaluation_version": EVALUATION_VERSION,
    }


def migration_input_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    old_distribution: dict[str, int] = {}
    new_distribution = {action.value: 0 for action in ActionCode}
    sentiment_counts = {sentiment.value: 0 for sentiment in SentimentCode}
    reclassified = 0
    group_for_action = {
        ActionCode.BUY: "买入类",
        ActionCode.INCREASE: "买入类",
        ActionCode.SELL: "卖出类",
        ActionCode.REDUCE: "卖出类",
        ActionCode.HOLD: "持有/观望类",
        ActionCode.HOLD_WATCH: "持有/观望类",
        ActionCode.OBSERVE: "持有/观望类",
        ActionCode.UNKNOWN: "unknown",
    }
    for record in records:
        old_group = str(record.get("action_group") or "unknown")
        old_distribution[old_group] = old_distribution.get(old_group, 0) + 1
        action = normalize_action(record.get("action_raw", record.get("action")))
        sentiment = normalize_sentiment(record.get("sentiment_raw", record.get("sentiment")))
        new_distribution[action.value] = new_distribution.get(action.value, 0) + 1
        sentiment_counts[sentiment.value] = sentiment_counts.get(sentiment.value, 0) + 1
        if old_group != "unknown" and old_group != group_for_action[action]:
            reclassified += 1
    return {
        "original_count": len(records),
        "raw_records_preserved": len(records),
        "old_action_group_distribution": old_distribution,
        "new_action_distribution": new_distribution,
        "new_sentiment_distribution": sentiment_counts,
        "reclassified_count": reclassified,
    }


def _validate_history_manifest(
    *,
    records: list[dict[str, Any]],
    raw_bytes: bytes,
    manifest: dict[str, Any],
    source: str,
) -> None:
    if int(manifest.get("count", -1)) != len(records):
        raise DataIntegrityError(
            f"history count mismatch for {source}: manifest={manifest.get('count')} actual={len(records)}"
        )
    expected_hash = str(manifest.get("sha256") or "").strip().lower()
    actual_hash = sha256_bytes(raw_bytes)
    if not expected_hash or expected_hash != actual_hash:
        raise DataIntegrityError(
            f"history sha256 mismatch for {source}: expected={expected_hash or 'missing'} actual={actual_hash}"
        )


def _read_local_history_with_contract(history_path: Path, manifest_path: Path) -> HistoryLoadResult | None:
    if not history_path.exists():
        return None
    raw = history_path.read_bytes()
    records = read_jsonl_strict_bytes(raw, source=str(history_path))
    if manifest_path.exists():
        manifest = read_json_strict(manifest_path)
        if not isinstance(manifest, dict):
            raise DataIntegrityError(f"invalid history manifest object: {manifest_path}")
        _validate_history_manifest(records=records, raw_bytes=raw, manifest=manifest, source=str(history_path))
        manifest = dict(manifest)
        manifest["migration_input_stats"] = migration_input_stats(records)
        return HistoryLoadResult(merge_history(records), "verified_local", str(history_path), manifest)

    # Compatibility migration: an old local accuracy payload is accepted only
    # when its declared total exactly matches the strict JSONL record count.
    accuracy_path: Path | None = None
    if history_path == LOCAL_HISTORY_PATH:
        accuracy_path = LOCAL_ACCURACY_PATH
    elif history_path == SITE_DATA_HISTORY_PATH:
        accuracy_path = SITE_DATA_ACCURACY_PATH
    if accuracy_path is not None and accuracy_path.exists():
        accuracy = read_json_strict(accuracy_path)
        declared = (
            accuracy.get("summary_all_history", {}).get("total_advice")
            if isinstance(accuracy, dict)
            else None
        )
        if isinstance(declared, int) and declared == len(records) and records:
            manifest = build_history_manifest(merge_history(records))
            manifest["migration_input_stats"] = migration_input_stats(records)
            return HistoryLoadResult(
                merge_history(records),
                "verified_legacy_local",
                str(history_path),
                manifest,
            )
    raise DataIntegrityError(
        f"history manifest missing for {history_path}; refusing to overwrite unverified history"
    )


def _fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "daily-stock-analysis-advice-backtest"})
    with urlopen(request, timeout=20) as response:
        return response.read()


def fetch_pages_history(
    history_url: str = PAGES_HISTORY_URL,
    manifest_url: str = PAGES_HISTORY_MANIFEST_URL,
    accuracy_url: str = PAGES_ACCURACY_URL,
) -> HistoryLoadResult:
    try:
        raw = _fetch_bytes(history_url)
    except Exception as exc:
        raise DataIntegrityError(f"cannot fetch previous Pages advice history: {type(exc).__name__}: {exc}") from exc
    records = read_jsonl_strict_bytes(raw, source=history_url)
    try:
        manifest_payload = json.loads(_fetch_bytes(manifest_url).decode("utf-8-sig"))
    except Exception as manifest_exc:
        # One-time compatibility for the pre-manifest deployment. The previous
        # public accuracy model is an independent count witness; it must match.
        try:
            accuracy = json.loads(_fetch_bytes(accuracy_url).decode("utf-8-sig"))
            declared = accuracy.get("summary_all_history", {}).get("total_advice")
        except Exception as accuracy_exc:
            raise DataIntegrityError(
                "previous history manifest is unavailable and legacy count witness could not be verified: "
                f"manifest={type(manifest_exc).__name__}; accuracy={type(accuracy_exc).__name__}"
            ) from accuracy_exc
        if not records or not isinstance(declared, int) or declared != len(records):
            raise DataIntegrityError(
                "previous history manifest is unavailable and legacy count witness does not match: "
                f"declared={declared!r} actual={len(records)}"
            )
        merged = merge_history(records)
        manifest = build_history_manifest(merged)
        manifest["migration_input_stats"] = migration_input_stats(records)
        return HistoryLoadResult(
            merged,
            "verified_legacy_remote",
            history_url,
            manifest,
        )

    if not isinstance(manifest_payload, dict):
        raise DataIntegrityError(f"invalid Pages history manifest object: {manifest_url}")
    _validate_history_manifest(
        records=records,
        raw_bytes=raw,
        manifest=manifest_payload,
        source=history_url,
    )
    manifest_payload = dict(manifest_payload)
    manifest_payload["migration_input_stats"] = migration_input_stats(records)
    return HistoryLoadResult(merge_history(records), "verified_remote", history_url, manifest_payload)


def load_history(*, allow_bootstrap_empty_history: bool = False) -> HistoryLoadResult:
    candidates = (
        (LOCAL_HISTORY_PATH, LOCAL_HISTORY_MANIFEST_PATH),
        (SITE_DATA_HISTORY_PATH, SITE_DATA_HISTORY_MANIFEST_PATH),
    )
    for history_path, manifest_path in candidates:
        local = _read_local_history_with_contract(history_path, manifest_path)
        if local is not None:
            return local
    try:
        return fetch_pages_history()
    except DataIntegrityError:
        if not allow_bootstrap_empty_history:
            raise
    manifest = build_history_manifest([])
    return HistoryLoadResult([], "bootstrap_empty_explicit", "explicit_cli", manifest)


def sanitize_history_record(record: dict[str, Any]) -> dict[str, Any]:
    # Legacy action/sentiment are migrated byte-for-byte into explicit raw
    # fields; only derived fields are recomputed by the current evaluator.
    action_raw = record.get("action_raw", record.get("action", ""))
    sentiment_raw = record.get("sentiment_raw", record.get("sentiment", ""))
    account_values = record.get("accounts")
    if not isinstance(account_values, list):
        account_values = [record.get("account")] if record.get("account") else []
    base = dict(record)
    base["schema_version"] = SCHEMA_VERSION
    base["evaluation_version"] = EVALUATION_VERSION
    base["code"] = normalize_code(record.get("code"))
    base["type"] = clean_text(record.get("type")) or REPORTABLE_TYPE
    base["name"] = clean_text(record.get("name")) or base["code"]
    base["accounts"] = sorted({clean_text(value) for value in account_values if clean_text(value)})
    base["action_raw"] = str(action_raw or "")
    base["sentiment_raw"] = str(sentiment_raw or "")
    base["action_normalized"] = normalize_action(action_raw).value
    base["sentiment_normalized"] = normalize_sentiment(sentiment_raw).value
    base["summary_raw"] = str(record.get("summary_raw", record.get("summary", "")) or "")
    base["summary"] = compact_summary(base["summary_raw"])
    base["date"] = clean_text(record.get("date") or record.get("report_date"))
    base["report_date"] = clean_text(record.get("report_date") or base["date"])
    base["anchor_session"] = clean_text(record.get("anchor_session")) or base["date"]
    if not record.get("anchor_precision"):
        base["anchor_precision"] = "legacy_date_only"
        base["anchor_assumption"] = (
            "旧记录仅保留日报日期；以该日期不晚于当日的最近交易时段作为兼容锚点。"
        )
    base["recommendation_id"] = clean_text(record.get("recommendation_id")) or (
        f"legacy:{base['date']}:{base['code']}"
    )
    base["run_id"] = clean_text(record.get("run_id")) or "legacy"
    base["revision"] = int(record.get("revision") or 1)
    base["official"] = bool(record.get("official", True))
    return public_advice_record(base)


def merge_history(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        cleaned = sanitize_history_record(record)
        date_text = clean_text(cleaned.get("date"))
        code = normalize_code(cleaned.get("code"))
        if not date_text or not code:
            continue
        if cleaned.get("type") != REPORTABLE_TYPE:
            continue
        cleaned["date"] = date_text
        cleaned["code"] = code
        key = (date_text, code)
        # One official post-close recommendation per date/security. Exact
        # retries are idempotent; a conflicting same-day revision is rejected
        # instead of silently replacing the official recommendation.
        existing = merged.get(key)
        if existing is None:
            merged[key] = cleaned
            continue
        official_fields = (
            "action_raw",
            "sentiment_raw",
            "score",
            "summary_raw",
            "anchor_session",
        )
        conflicts = [
            field
            for field in official_fields
            if existing.get(field) != cleaned.get(field)
        ]
        if conflicts:
            raise DataIntegrityError(
                "conflicting same-day official recommendation: "
                f"date={date_text} code={code} fields={conflicts}; "
                "the first official post-close recommendation remains authoritative"
            )
    return sorted(
        merged.values(),
        key=lambda item: (item.get("date", ""), item.get("code", ""), item.get("recommendation_id", "")),
    )


def merge_new_official_records(
    history: Iterable[dict[str, Any]],
    new_records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep the first official post-close recommendation for a security/session.

    Stored history is validated strictly by ``merge_history``. A later workflow
    run on the same session is a recovery run, not a silent recommendation
    revision, so it cannot replace the previously published raw advice.
    """

    existing = merge_history(history)
    by_key = {(str(item.get("date") or ""), normalize_code(item.get("code"))): item for item in existing}
    added: list[dict[str, Any]] = []
    exact_retries = 0
    conflicting_retries = 0
    official_fields = ("action_raw", "sentiment_raw", "score", "summary_raw", "anchor_session")

    for raw_record in new_records:
        record = sanitize_history_record(raw_record)
        key = (str(record.get("date") or ""), normalize_code(record.get("code")))
        previous = by_key.get(key)
        if previous is None:
            by_key[key] = record
            added.append(record)
            continue
        if all(previous.get(field) == record.get(field) for field in official_fields):
            exact_retries += 1
        else:
            conflicting_retries += 1

    merged = sorted(
        by_key.values(),
        key=lambda item: (item.get("date", ""), item.get("code", ""), item.get("recommendation_id", "")),
    )
    return merged, {
        "added": len(added),
        "exact_retries_skipped": exact_retries,
        "conflicting_retries_skipped": conflicting_retries,
    }


def dataframe_to_bars(df: Any) -> list[DailyBar]:
    if df is None or getattr(df, "empty", True):
        return []
    bars: list[DailyBar] = []
    columns = {str(col).strip().lower(): col for col in getattr(df, "columns", [])}

    def pick_column(candidates: Iterable[str]) -> Any:
        for candidate in candidates:
            column = columns.get(candidate.strip().lower())
            if column is not None:
                return column
        return None

    date_col = (
        pick_column(
            (
                "date",
                "trade_date",
                "datetime",
                "日期",
                "交易日期",
                "时间",
            )
        )
    )
    close_col = (
        pick_column(
            (
                "close",
                "Close",
                "收盘",
                "收盘价",
                "收盘价(元)",
                "最新价",
            )
        )
    )
    if date_col is None or close_col is None:
        return []
    for _, row in df.iterrows():
        raw_date = row.get(date_col)
        raw_close = row.get(close_col)
        parsed_date = parse_date(raw_date)
        close = parse_float(raw_close)
        if parsed_date and close is not None and close > 0:
            bars.append(DailyBar(parsed_date, close))
    return sorted({bar.trade_date: bar for bar in bars}.values(), key=lambda bar: bar.trade_date)


def parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def classify_advice(record: dict[str, Any]) -> str:
    return normalize_action(record.get("action_raw", record.get("action"))).value


def evaluate_direction(action: ActionCode, return_value: float) -> tuple[bool | None, str]:
    if action in {ActionCode.BUY, ActionCode.INCREASE}:
        return return_value > 0, "方向性买入/加仓后下跌" if return_value <= 0 else ""
    if action in {ActionCode.SELL, ActionCode.REDUCE}:
        return return_value < 0, "方向性卖出/减仓后上涨" if return_value >= 0 else ""
    return None, ""


def evaluate_sentiment(sentiment: SentimentCode, period: str, return_value: float) -> bool | None:
    if sentiment == SentimentCode.BULLISH:
        return return_value > 0
    if sentiment == SentimentCode.BEARISH:
        return return_value < 0
    if sentiment == SentimentCode.NEUTRAL:
        band = HOLD_BAND[period]
        return -band <= return_value <= band
    return None


def bars_date_range_text(bars: list[DailyBar]) -> str:
    if not bars:
        return "none"
    return f"{bars[0].trade_date.isoformat()}~{bars[-1].trade_date.isoformat()}"


def price_diagnostic(
    *,
    code: str,
    analysis_date: date,
    period: str | None,
    missing: str,
    bars: list[DailyBar],
    error: str | None = None,
    advice_close_date: date | None = None,
) -> str:
    formats = ", ".join(DataFetcherPriceProvider._code_variants(code))
    parts = [
        f"code={code}",
        f"advice_date={analysis_date.isoformat()}",
    ]
    if period:
        parts.append(f"horizon={period}")
    if advice_close_date:
        parts.append(f"advice_close_date={advice_close_date.isoformat()}")
    parts.extend(
        [
            f"missing={missing}",
            f"compatible_formats={formats}",
            f"daily_rows={len(bars)}",
            f"available_dates={bars_date_range_text(bars)}",
        ]
    )
    if error:
        compact_error = re.sub(r"\s+", " ", str(error)).strip()
        if len(compact_error) > 240:
            compact_error = compact_error[:237].rstrip() + "..."
        parts.append(f"provider_error={compact_error}")
    return "；".join(parts)


def _legacy_anchor_session(analysis_date: date, calendar: SessionCalendar) -> date:
    search_start = analysis_date - timedelta(days=45)
    sessions = [session for session in calendar.sessions_between(search_start, analysis_date) if session <= analysis_date]
    if not sessions:
        raise SessionCalendarUnavailable(
            f"no completed A-share session available for legacy advice date {analysis_date.isoformat()}"
        )
    return sessions[-1]


def _clear_derived_fields(result: dict[str, Any]) -> None:
    legacy_suffixes = ("_hit", "_miss_reason")
    current_suffixes = (
        "_direction_hit",
        "_direction_miss_reason",
        "_sentiment_aligned",
        "_observe_consistent",
        "_observe_reason",
        "_hold_drawdown_flag",
    )
    for period in PERIODS:
        for suffix in (*legacy_suffixes, *current_suffixes):
            result.pop(f"{period}{suffix}", None)


def _apply_evaluation_semantics(
    result: dict[str, Any],
    *,
    period: str,
    return_value: float,
    action: ActionCode,
    sentiment: SentimentCode,
) -> None:
    direction_hit, direction_reason = evaluate_direction(action, return_value)
    result[f"{period}_direction_hit"] = direction_hit
    if direction_reason:
        result[f"{period}_direction_miss_reason"] = direction_reason
    result[f"{period}_sentiment_aligned"] = evaluate_sentiment(sentiment, period, return_value)
    if action == ActionCode.OBSERVE:
        band = HOLD_BAND[period]
        consistent = -band <= return_value <= band
        result[f"{period}_observe_consistent"] = consistent
        if not consistent:
            result[f"{period}_observe_reason"] = (
                "未捕捉上涨机会" if return_value > band else "未识别下跌风险"
            )
    else:
        result[f"{period}_observe_consistent"] = None
    if action in {ActionCode.HOLD, ActionCode.HOLD_WATCH}:
        result[f"{period}_hold_drawdown_flag"] = return_value < -HOLD_BAND[period]
    else:
        result[f"{period}_hold_drawdown_flag"] = None


def evaluate_record(
    record: dict[str, Any],
    provider: PriceProvider,
    *,
    calendar: SessionCalendar | None = None,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    result = sanitize_history_record(record)
    result.pop("price_warning", None)
    _clear_derived_fields(result)
    result["evaluation_version"] = EVALUATION_VERSION
    analysis_date = parse_date(result.get("date"))
    action = normalize_action(result.get("action_raw"))
    sentiment = normalize_sentiment(result.get("sentiment_raw"))
    result["action_normalized"] = action.value
    result["sentiment_normalized"] = sentiment.value
    if analysis_date is None:
        result["failure_code"] = FailureCode.INVALID_RESPONSE.value
        for period in PERIODS:
            result[f"{period}_status"] = "数据不足"
        return public_advice_record(result)

    session_calendar = calendar or ExchangeSessionCalendar()
    now = current_time or datetime.now(SHANGHAI_TZ)
    if now.tzinfo is None:
        raise ValueError("current_time must be timezone-aware")
    through_session = session_calendar.completed_session_at(now)
    explicit_anchor = parse_date(result.get("anchor_session"))
    if result.get("anchor_precision") == "exact_session" and explicit_anchor:
        anchor_session = explicit_anchor
    else:
        anchor_session = _legacy_anchor_session(analysis_date, session_calendar)
        result["anchor_session"] = anchor_session.isoformat()
        result["anchor_precision"] = "legacy_date_only"
        result["anchor_assumption"] = (
            "旧记录无精确生成时刻；使用日报日期不晚于当日的最近正式交易时段。"
        )

    code = normalize_code(result.get("code"))
    cached_advice_close = parse_float(result.get("advice_close"))
    target_closes_available = all(
        parse_float(result.get(f"{period}_close")) is not None
        and parse_date(result.get(f"{period}_date")) is not None
        for period in PERIODS
        if nth_session_after(session_calendar, anchor_session, PERIODS[period], through_session) is not None
    )
    needs_prices = cached_advice_close is None or cached_advice_close <= 0 or not target_closes_available
    bars: list[DailyBar] = []
    error: str | None = None
    if needs_prices:
        bars, error = provider.get_bars(code, anchor_session)
        bars = sorted(
            {bar.trade_date: bar for bar in bars if bar.close > 0}.values(),
            key=lambda bar: bar.trade_date,
        )
    bar_by_date = {bar.trade_date: bar for bar in bars}

    advice_close = cached_advice_close
    if advice_close is None or advice_close <= 0:
        anchor_bar = bar_by_date.get(anchor_session)
        advice_close = anchor_bar.close if anchor_bar else None
    if advice_close is None or advice_close <= 0:
        result["failure_code"] = FailureCode.MARKET_DATA_MISSING.value
        result["price_warning"] = price_diagnostic(
            code=code,
            analysis_date=analysis_date,
            period=None,
            missing="advice_close",
            bars=bars,
            error=error,
            advice_close_date=anchor_session,
        )
        for period, offset in PERIODS.items():
            target_session = nth_session_after(session_calendar, anchor_session, offset, through_session)
            result[f"{period}_status"] = "等待验证" if target_session is None else "数据不足"
            result[f"{period}_close"] = None
            result[f"{period}_return"] = None
        return public_advice_record(result)

    result["advice_close"] = round(advice_close, 4)
    result["advice_close_date"] = anchor_session.isoformat()
    result["failure_code"] = FailureCode.NONE.value
    for period, offset in PERIODS.items():
        target_session = nth_session_after(session_calendar, anchor_session, offset, through_session)
        if target_session is None:
            result[f"{period}_status"] = "等待验证"
            result[f"{period}_close"] = None
            result[f"{period}_date"] = None
            result[f"{period}_return"] = None
            continue

        existing_date = parse_date(result.get(f"{period}_date"))
        existing_close = parse_float(result.get(f"{period}_close"))
        if existing_date != target_session or existing_close is None or existing_close <= 0:
            target_bar = bar_by_date.get(target_session)
            target_close = target_bar.close if target_bar else None
        else:
            target_close = existing_close
        if target_close is None or target_close <= 0:
            result["failure_code"] = FailureCode.MARKET_DATA_MISSING.value
            result["price_warning"] = price_diagnostic(
                code=code,
                analysis_date=analysis_date,
                period=period,
                missing="target_close",
                bars=bars,
                error=error,
                advice_close_date=anchor_session,
            )
            result[f"{period}_status"] = "数据不足"
            result[f"{period}_close"] = None
            result[f"{period}_date"] = target_session.isoformat()
            result[f"{period}_return"] = None
            continue

        return_value = (target_close - advice_close) / advice_close
        result[f"{period}_status"] = "已验证"
        result[f"{period}_close"] = round(target_close, 4)
        result[f"{period}_date"] = target_session.isoformat()
        result[f"{period}_return"] = round(return_value, 6)
        _apply_evaluation_semantics(
            result,
            period=period,
            return_value=return_value,
            action=action,
            sentiment=sentiment,
        )
    return public_advice_record(result)


def evaluate_records(
    history: list[dict[str, Any]],
    provider: PriceProvider,
    current_codes: set[str],
    *,
    calendar: SessionCalendar | None = None,
    current_time: datetime | None = None,
) -> list[dict[str, Any]]:
    evaluated = []
    ordered_history = sorted(
        history,
        key=lambda item: (str(item.get("date") or ""), normalize_code(item.get("code"))),
    )
    for record in ordered_history:
        item = evaluate_record(
            record,
            provider,
            calendar=calendar,
            current_time=current_time,
        )
        item["is_current_holding_now"] = item.get("code") in current_codes
        evaluated.append(item)
    return sorted(evaluated, key=lambda item: (item.get("date", ""), item.get("code", "")))


def summarize_status(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"total_advice": len(records)}
    for period in PERIODS:
        evaluated = [r for r in records if r.get(f"{period}_status") == "已验证"]
        waiting = sum(1 for r in records if r.get(f"{period}_status") == "等待验证")
        insufficient = sum(1 for r in records if r.get(f"{period}_status") == "数据不足")
        summary[f"{period}_evaluated"] = len(evaluated)
        summary[f"{period}_waiting"] = waiting
        summary[f"{period}_insufficient"] = insufficient
    return summary


def binary_metric_summary(records: list[dict[str, Any]], field_suffix: str) -> dict[str, Any]:
    summary = summarize_status(records)
    for period in PERIODS:
        values = [
            record.get(f"{period}_{field_suffix}")
            for record in records
            if record.get(f"{period}_status") == "已验证"
            and isinstance(record.get(f"{period}_{field_suffix}"), bool)
        ]
        positive = sum(value is True for value in values)
        summary[f"{period}_sample_size"] = len(values)
        summary[f"{period}_positive"] = positive
        summary[f"{period}_rate"] = round(positive / len(values), 4) if values else None
    return summary


def hold_result_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize_status(records)
    for period in PERIODS:
        returns = [
            float(record[f"{period}_return"])
            for record in records
            if record.get(f"{period}_status") == "已验证"
            and parse_float(record.get(f"{period}_return")) is not None
        ]
        drawdowns = sum(value < -HOLD_BAND[period] for value in returns)
        negatives = sum(value < 0 for value in returns)
        summary[f"{period}_sample_size"] = len(returns)
        summary[f"{period}_median_return"] = (
            round(float(statistics.median(returns)), 6) if returns else None
        )
        summary[f"{period}_negative_rate"] = round(negatives / len(returns), 4) if returns else None
        summary[f"{period}_material_drawdown_count"] = drawdowns
        summary[f"{period}_material_drawdown_rate"] = (
            round(drawdowns / len(returns), 4) if returns else None
        )
    return summary


def action_distribution(records: list[dict[str, Any]]) -> dict[str, int]:
    distribution = {action.value: 0 for action in ActionCode}
    for record in records:
        action = normalize_action(record.get("action_raw"))
        distribution[action.value] = distribution.get(action.value, 0) + 1
    return distribution


def sentiment_distribution(records: list[dict[str, Any]]) -> dict[str, int]:
    distribution = {sentiment.value: 0 for sentiment in SentimentCode}
    for record in records:
        sentiment = normalize_sentiment(record.get("sentiment_raw"))
        distribution[sentiment.value] = distribution.get(sentiment.value, 0) + 1
    return distribution


def build_metric_set(records: list[dict[str, Any]]) -> dict[str, Any]:
    directional = [
        record
        for record in records
        if normalize_action(record.get("action_raw"))
        in {ActionCode.BUY, ActionCode.INCREASE, ActionCode.SELL, ActionCode.REDUCE}
    ]
    holds = [
        record
        for record in records
        if normalize_action(record.get("action_raw")) in {ActionCode.HOLD, ActionCode.HOLD_WATCH}
    ]
    observes = [
        record for record in records if normalize_action(record.get("action_raw")) == ActionCode.OBSERVE
    ]
    sentiments = [
        record
        for record in records
        if normalize_sentiment(record.get("sentiment_raw")) != SentimentCode.UNKNOWN
    ]
    return {
        "directional_action": binary_metric_summary(directional, "direction_hit"),
        "sentiment_alignment": binary_metric_summary(sentiments, "sentiment_aligned"),
        "hold_results": hold_result_summary(holds),
        "observe_consistency": binary_metric_summary(observes, "observe_consistent"),
        "action_distribution": action_distribution(records),
        "sentiment_distribution": sentiment_distribution(records),
    }


def build_accuracy(records: list[dict[str, Any]]) -> dict[str, Any]:
    current_records = [record for record in records if record.get("is_current_holding_now")]
    miss_cases = []
    for record in sorted(records, key=lambda item: item.get("date", ""), reverse=True):
        for period in PERIODS:
            action = normalize_action(record.get("action_raw"))
            direction_miss = record.get(f"{period}_direction_hit") is False
            observe_miss = record.get(f"{period}_observe_consistent") is False
            if direction_miss or observe_miss:
                item = {
                    "date": record.get("date"),
                    "code": record.get("code"),
                    "name": record.get("name"),
                    "accounts": record.get("accounts", []),
                    "action_raw": record.get("action_raw"),
                    "action_normalized": action.value,
                    "sentiment_raw": record.get("sentiment_raw"),
                    "period": period,
                    "return": record.get(f"{period}_return"),
                    "miss_reason": (
                        record.get(f"{period}_direction_miss_reason")
                        or record.get(f"{period}_observe_reason")
                        or "未一致"
                    ),
                    "is_current_holding_now": record.get("is_current_holding_now"),
                }
                miss_cases.append(item)
                break

    return {
        "updated_at": now_iso(),
        "schema_version": 2,
        "evaluation_version": EVALUATION_VERSION,
        "neutral_band": dict(HOLD_BAND),
        "summary_all_history": summarize_status(records),
        "summary_current_holdings": summarize_status(current_records),
        "metrics_all_history": build_metric_set(records),
        "metrics_current_holdings": build_metric_set(current_records),
        "records": records,
        "recent_records": sorted(records, key=lambda item: (item.get("date", ""), item.get("code", "")), reverse=True)[:20],
        "miss_cases": miss_cases[:20],
    }


def build_accuracy_with_metadata(
    records: list[dict[str, Any]],
    *,
    latest_report_date: str | None,
    latest_report_name: str | None,
    new_advice_count: int,
) -> dict[str, Any]:
    accuracy = build_accuracy(records)
    accuracy["latest_report_date"] = latest_report_date
    accuracy["latest_report_name"] = latest_report_name
    accuracy["new_advice_count"] = new_advice_count
    accuracy["new_advice_message"] = (
        f"本次新增 {new_advice_count} 条可回测建议。"
        if new_advice_count
        else "本次无新增可回测建议。"
    )
    return accuracy


def format_rate(value: Any) -> str:
    if value is None:
        return "样本不足"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "样本不足"


def format_return(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def status_text(record: dict[str, Any], period: str) -> str:
    status = str(record.get(f"{period}_status") or "等待验证")
    if status != "已验证":
        return status
    action = normalize_action(record.get("action_raw"))
    if action in {ActionCode.BUY, ActionCode.INCREASE, ActionCode.SELL, ActionCode.REDUCE}:
        hit = record.get(f"{period}_direction_hit")
        return "方向命中" if hit is True else str(record.get(f"{period}_direction_miss_reason") or "方向未命中")
    if action == ActionCode.OBSERVE:
        consistent = record.get(f"{period}_observe_consistent")
        return "区间一致" if consistent is True else str(record.get(f"{period}_observe_reason") or "区间不一致")
    if action in {ActionCode.HOLD, ActionCode.HOLD_WATCH}:
        return "出现明显回撤" if record.get(f"{period}_hold_drawdown_flag") else "已验证"
    return "已验证"


def markdown_summary_line(summary: dict[str, Any], label: str) -> list[str]:
    return [
        f"### {label}",
        "",
        f"- 已记录建议数量：{summary.get('total_advice', 0)}",
        f"- T+1：已验证 {summary.get('d1_evaluated', 0)}，等待 {summary.get('d1_waiting', 0)}，数据不足 {summary.get('d1_insufficient', 0)}",
        f"- T+5：已验证 {summary.get('d5_evaluated', 0)}，等待 {summary.get('d5_waiting', 0)}，数据不足 {summary.get('d5_insufficient', 0)}",
        f"- T+20：已验证 {summary.get('d20_evaluated', 0)}，等待 {summary.get('d20_waiting', 0)}，数据不足 {summary.get('d20_insufficient', 0)}",
        "",
    ]


def render_markdown(accuracy: dict[str, Any], report_date: str) -> str:
    records = sorted(
        list(accuracy.get("records", [])),
        key=lambda item: (str(item.get("date") or ""), str(item.get("code") or "")),
        reverse=True,
    )
    lines = [
        f"# {report_date} AI 建议准确性回测",
        "",
        "本报告不调用 Gemini 或任何 LLM，只回看历史建议与后续真实行情的一致性。",
        "",
        f"- 最新读取日报：{accuracy.get('latest_report_date') or '暂无'}",
        f"- {accuracy.get('new_advice_message') or '本次无新增可回测建议。'}",
        "",
        "## 总览",
        "",
    ]
    lines.extend(markdown_summary_line(accuracy.get("summary_all_history", {}), "全部历史建议"))
    lines.extend(markdown_summary_line(accuracy.get("summary_current_holdings", {}), "当前持仓建议"))

    lines.extend(["## 分语义统计", ""])
    metric_labels = {
        "directional_action": "方向性动作命中率",
        "sentiment_alignment": "情绪方向一致率",
        "hold_results": "持有结果",
        "observe_consistency": "观望区间一致率",
    }
    for key, label in metric_labels.items():
        summary = accuracy.get("metrics_all_history", {}).get(key, {})
        if key == "hold_results":
            lines.append(
                f"- {label}：样本 {summary.get('total_advice', 0)}；"
                f"T+1 中位收益 {format_return(summary.get('d1_median_return'))}（n={summary.get('d1_sample_size', 0)}），"
                f"T+5 中位收益 {format_return(summary.get('d5_median_return'))}（n={summary.get('d5_sample_size', 0)}），"
                f"T+20 中位收益 {format_return(summary.get('d20_median_return'))}（n={summary.get('d20_sample_size', 0)}）"
            )
        else:
            lines.append(
                f"- {label}：样本 {summary.get('total_advice', 0)}；"
                f"T+1 {format_rate(summary.get('d1_rate'))}（n={summary.get('d1_sample_size', 0)}），"
                f"T+5 {format_rate(summary.get('d5_rate'))}（n={summary.get('d5_sample_size', 0)}），"
                f"T+20 {format_rate(summary.get('d20_rate'))}（n={summary.get('d20_sample_size', 0)}）"
            )
    lines.append("")

    lines.extend(["## 当前持仓建议回看", ""])
    current = [record for record in records if record.get("is_current_holding_now")]
    if not current:
        lines.append("暂无当前持仓建议样本。")
    for record in current:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action_raw')}，T+1 {status_text(record, 'd1')}，T+5 {status_text(record, 'd5')}，T+20 {status_text(record, 'd20')}")
    lines.append("")

    lines.extend(["## 最近建议回看", ""])
    recent_records = list(accuracy.get("recent_records", []))
    if not recent_records:
        lines.append("暂无最近建议样本。")
    for record in recent_records:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action_raw')} / {record.get('sentiment_raw')}，T+1 {status_text(record, 'd1')}，T+5 {status_text(record, 'd5')}，T+20 {status_text(record, 'd20')}")
    lines.append("")

    lines.extend(["## 历史全部建议回测", ""])
    if not records:
        lines.append("暂无历史建议样本。")
    for record in records:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action_raw')} / {record.get('sentiment_raw')}，T+1 {status_text(record, 'd1')}，T+5 {status_text(record, 'd5')}，T+20 {status_text(record, 'd20')}")
    lines.append("")

    lines.extend(["## 最近错误案例", ""])
    miss_cases = accuracy.get("miss_cases", [])
    if not miss_cases:
        lines.append("暂无已验证未命中样本。")
    for record in miss_cases[:20]:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action_raw')}，{record.get('period')} {record.get('miss_reason')}，收益率 {format_return(record.get('return'))}")
    lines.append("")

    lines.extend([
        "## 数据不足说明",
        "",
        "- T+1 / T+5 / T+20 使用建议锚点之后第 N 个正式 A 股交易时段。",
        "- 尚未到达验证窗口时显示“等待验证”。",
        "- 行情数据源缺少建议日或后续收盘价时显示“数据不足”。",
        "",
        DISCLAIMER,
        "",
    ])
    return "\n".join(lines)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    write_jsonl_atomic(path, [public_advice_record(record) for record in records])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(path, payload)


def write_outputs(history: list[dict[str, Any]], accuracy: dict[str, Any], report_date: str) -> None:
    public_history = [public_advice_record(record) for record in history]
    manifest = build_history_manifest(public_history)
    write_jsonl(LOCAL_HISTORY_PATH, public_history)
    write_jsonl(SITE_DATA_HISTORY_PATH, public_history)
    write_json(LOCAL_HISTORY_MANIFEST_PATH, manifest)
    write_json(SITE_DATA_HISTORY_MANIFEST_PATH, manifest)
    write_json(LOCAL_ACCURACY_PATH, accuracy)
    write_json(SITE_DATA_ACCURACY_PATH, accuracy)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    markdown_path = REPORTS_DIR / f"advice_accuracy_{report_date.replace('-', '')}.md"
    markdown_path.write_text(render_markdown(accuracy, report_date), encoding="utf-8")

    print(f"Advice history written: {LOCAL_HISTORY_PATH.relative_to(ROOT_DIR)}")
    print(f"Advice accuracy written: {LOCAL_ACCURACY_PATH.relative_to(ROOT_DIR)}")
    print("Advice HTML is owned by scripts/build_pages_report.py and was not written here.")
    print(f"Advice markdown report written: {markdown_path.relative_to(ROOT_DIR)}")


def run_backtest(
    provider: PriceProvider | None = None,
    *,
    calendar: SessionCalendar | None = None,
    current_time: datetime | None = None,
    allow_bootstrap_empty_history: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    holdings = load_current_stock_holdings()
    explicit_report = report_path.resolve() if report_path is not None else None
    if explicit_report is not None and not explicit_report.exists():
        raise DataIntegrityError(f"explicit stock report does not exist: {explicit_report}")
    structured_path = (
        explicit_report
        if explicit_report is not None and explicit_report.suffix.lower() == ".json"
        else latest_structured_stock_report()
    )
    legacy_path = (
        explicit_report
        if explicit_report is not None and explicit_report.suffix.lower() == ".md"
        else latest_stock_report()
    )
    report_path = structured_path or legacy_path
    if structured_path:
        new_records = extract_advice_from_structured_report(structured_path, holdings)
    elif legacy_path:
        print(
            "WARNING: using legacy Markdown advice adapter; new runs must produce report_YYYYMMDD.json",
            file=sys.stderr,
        )
        new_records = extract_advice_from_report(legacy_path, holdings)
    else:
        new_records = []
    history_source = load_history(allow_bootstrap_empty_history=allow_bootstrap_empty_history)
    history, official_merge = merge_new_official_records(history_source.records, new_records)
    if official_merge["conflicting_retries_skipped"]:
        print(
            "WARNING: skipped conflicting same-session recommendation retries; "
            "the first official post-close recommendation remains authoritative: "
            f"count={official_merge['conflicting_retries_skipped']}",
            file=sys.stderr,
        )
    current_codes = set(holdings)
    price_provider = provider or DataFetcherPriceProvider()
    session_calendar = calendar or ExchangeSessionCalendar()
    evaluated = evaluate_records(
        history,
        price_provider,
        current_codes,
        calendar=session_calendar,
        current_time=current_time,
    )
    if isinstance(price_provider, DataFetcherPriceProvider):
        print(
            "Price history requests: "
            f"{price_provider.request_count} for {len({item.get('code') for item in history if item.get('code')})} unique codes"
        )
    report_date = report_date_text(report_path) if report_path else datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    accuracy = build_accuracy_with_metadata(
        evaluated,
        latest_report_date=report_date if report_path else None,
        latest_report_name=report_path.name if report_path else None,
        new_advice_count=official_merge["added"],
    )
    accuracy["official_recommendation_policy"] = {
        "policy": "first_successful_post_close_per_session_security",
        **official_merge,
    }
    accuracy["history_source_status"] = history_source.status
    accuracy["history_source"] = history_source.source
    accuracy["previous_history_count"] = len(history_source.records)
    accuracy["history_manifest"] = {
        "count": history_source.manifest.get("count"),
        "sha256": history_source.manifest.get("sha256"),
        "evaluation_version": history_source.manifest.get("evaluation_version"),
    }
    input_stats = dict(history_source.manifest.get("migration_input_stats") or {})
    current_distribution = action_distribution(evaluated)
    accuracy["migration_stats"] = {
        **input_stats,
        "records_before": len(history_source.records),
        "records_after": len(evaluated),
        "raw_records_changed": 0,
        "new_action_distribution": current_distribution,
        "new_sentiment_distribution": sentiment_distribution(evaluated),
        "evaluation_version": EVALUATION_VERSION,
        "verified_samples": {
            period: sum(record.get(f"{period}_status") == "已验证" for record in evaluated)
            for period in PERIODS
        },
        "directional_samples": sum(
            normalize_action(record.get("action_raw"))
            in {ActionCode.BUY, ActionCode.INCREASE, ActionCode.SELL, ActionCode.REDUCE}
            for record in evaluated
        ),
        "hold_samples": sum(
            normalize_action(record.get("action_raw")) in {ActionCode.HOLD, ActionCode.HOLD_WATCH}
            for record in evaluated
        ),
        "observe_samples": sum(
            normalize_action(record.get("action_raw")) == ActionCode.OBSERVE
            for record in evaluated
        ),
    }
    write_outputs(evaluated, accuracy, report_date)
    return accuracy


def run_contract_self_test() -> None:
    action_cases = {
        "持有": ActionCode.HOLD,
        "持有观察": ActionCode.HOLD_WATCH,
        "观望": ActionCode.OBSERVE,
        "不建议买入": ActionCode.OBSERVE,
        "暂不卖出": ActionCode.HOLD,
        "不宜追涨": ActionCode.OBSERVE,
        "买入": ActionCode.BUY,
        "加仓": ActionCode.INCREASE,
        "卖出": ActionCode.SELL,
        "减仓": ActionCode.REDUCE,
        "": ActionCode.UNKNOWN,
        "无法识别": ActionCode.UNKNOWN,
    }
    for raw, expected in action_cases.items():
        assert normalize_action(raw) == expected, (raw, normalize_action(raw), expected)
    assert normalize_action("观望") == ActionCode.OBSERVE
    assert normalize_sentiment("强烈看多") == SentimentCode.BULLISH
    assert normalize_action("持有") == ActionCode.HOLD
    assert normalize_sentiment("偏空") == SentimentCode.BEARISH

    sessions = [
        date(2026, 9, 28),
        date(2026, 9, 29),
        date(2026, 9, 30),
        date(2026, 10, 9),
        date(2026, 10, 12),
        date(2026, 10, 13),
        date(2026, 10, 14),
        date(2026, 10, 15),
        date(2026, 10, 16),
    ]
    calendar = StaticSessionCalendar(sessions)
    anchor = date(2026, 9, 30)
    bars = [DailyBar(anchor, 10.0), DailyBar(date(2026, 10, 9), 10.5)]
    provider = MockPriceProvider({"600000": bars})
    waiting = evaluate_record(
        {
            "date": anchor.isoformat(),
            "anchor_session": anchor.isoformat(),
            "anchor_precision": "exact_session",
            "code": "600000",
            "type": "stock",
            "action_raw": "观望",
            "sentiment_raw": "看多",
        },
        provider,
        calendar=calendar,
        current_time=datetime(2026, 10, 8, 20, tzinfo=SHANGHAI_TZ),
    )
    assert waiting["d1_status"] == "等待验证"
    verified = evaluate_record(
        waiting,
        provider,
        calendar=calendar,
        current_time=datetime(2026, 10, 9, 20, tzinfo=SHANGHAI_TZ),
    )
    assert verified["d1_status"] == "已验证"
    assert verified["d1_observe_consistent"] is False
    assert verified["d1_sentiment_aligned"] is True
    assert "d1_direction_hit" in verified and verified["d1_direction_hit"] is None

    migrated = evaluate_record(
        {
            "date": anchor.isoformat(),
            "anchor_session": anchor.isoformat(),
            "anchor_precision": "exact_session",
            "code": "600001",
            "type": "stock",
            "action": "持有",
            "sentiment": "看空",
            "advice_close": 10.0,
            "d1_status": "已验证",
            "d1_date": "2026-10-09",
            "d1_close": 9.5,
            "d1_return": -0.05,
            "d1_hit": True,
            "evaluation_version": "1",
        },
        MockErrorPriceProvider(),
        calendar=calendar,
        current_time=datetime(2026, 10, 9, 20, tzinfo=SHANGHAI_TZ),
    )
    assert migrated["action_normalized"] == ActionCode.HOLD.value
    assert "d1_hit" not in migrated
    assert migrated["d1_hold_drawdown_flag"] is True
    assert migrated["d1_sentiment_aligned"] is True
    assert migrated["evaluation_version"] == EVALUATION_VERSION

    first = {"date": "2026-10-09", "code": "600002", "type": "stock", "action": "观望"}
    exact_retry = dict(first)
    merged = merge_history([first, exact_retry])
    assert len(merged) == 1 and merged[0]["action_raw"] == "观望"

    conflicting_retry = {
        "date": "2026-10-09",
        "code": "600002",
        "type": "stock",
        "action": "买入",
    }
    try:
        merge_history([first, conflicting_retry])
    except DataIntegrityError as exc:
        assert "conflicting same-day official recommendation" in str(exc)
    else:
        raise AssertionError("conflicting same-day recommendations must fail closed")

    merged, retry_stats = merge_new_official_records([first], [conflicting_retry])
    assert merged[0]["action_raw"] == "观望"
    assert retry_stats == {
        "added": 0,
        "exact_retries_skipped": 0,
        "conflicting_retries_skipped": 1,
    }

    try:
        read_jsonl_strict_bytes(b'{"ok": 1}\n{broken}\n', source="self-test")
    except DataIntegrityError as exc:
        assert ":2:" in str(exc)
    else:
        raise AssertionError("corrupted JSONL must fail closed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update AI advice backtest outputs without calling any LLM.")
    parser.add_argument("--test-mode", action="store_true", help="Create deterministic mock data and validate the module.")
    parser.add_argument(
        "--allow-bootstrap-empty-history",
        action="store_true",
        help="Explicitly initialize an empty history when no trusted previous history exists.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Explicit current-run report_YYYYMMDD.json (CI must pass this instead of guessing latest)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.test_mode:
        run_contract_self_test()
        print("advice backtest test mode passed")
        return 0

    try:
        run_backtest(
            allow_bootstrap_empty_history=args.allow_bootstrap_empty_history,
            report_path=args.report,
        )
    except Exception as exc:
        print(f"ERROR: advice backtest update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
