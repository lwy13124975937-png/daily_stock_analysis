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
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT_DIR / "reports"
DATA_DIR = ROOT_DIR / "data"
SITE_DIR = ROOT_DIR / "site"
SITE_DATA_DIR = SITE_DIR / "data"
SITE_DATA_HISTORY_PATH = SITE_DATA_DIR / "advice_history.jsonl"
SITE_DATA_ACCURACY_PATH = SITE_DATA_DIR / "advice_accuracy.json"
LOCAL_HISTORY_PATH = DATA_DIR / "advice_history.jsonl"
LOCAL_ACCURACY_PATH = DATA_DIR / "advice_accuracy.json"
SNAPSHOT_PATH = ROOT_DIR / "site_data" / "holdings_snapshot.json"
CURRENT_STOCK_LIST_PATH = ROOT_DIR / "site_data" / "current_stock_list.json"
PAGES_HISTORY_URL = (
    "https://lwy13124975937-png.github.io/"
    "daily_stock_analysis/data/advice_history.jsonl"
)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
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
    account: str
    type: str


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


def now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def now_text() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def report_date_text(report_path: Path) -> str:
    match = re.search(r"(20\d{6})", report_path.stem)
    if not match:
        return datetime.fromtimestamp(report_path.stat().st_mtime, SHANGHAI_TZ).strftime("%Y-%m-%d")
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


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict:
    if not path.exists():
        print(f"WARNING: holdings snapshot not found: {path}", file=sys.stderr)
        return {}
    try:
        return read_json(path)
    except Exception as exc:
        print(f"WARNING: cannot read holdings snapshot {path}: {exc}", file=sys.stderr)
        return {}


def load_current_stock_list(path: Path = CURRENT_STOCK_LIST_PATH) -> dict[str, Holding]:
    if not path.exists():
        print(f"WARNING: current stock list not found: {path}", file=sys.stderr)
        return {}
    try:
        payload = read_json(path)
    except Exception as exc:
        print(f"WARNING: cannot read current stock list {path}: {exc}", file=sys.stderr)
        return {}
    stocks = payload.get("stocks", []) if isinstance(payload, dict) else []
    if not isinstance(stocks, list):
        return {}

    holdings: dict[str, Holding] = {}
    for item in stocks:
        if not isinstance(item, dict):
            continue
        if clean_text(item.get("type")).lower() != REPORTABLE_TYPE:
            continue
        code = normalize_code(item.get("code"))
        if not code:
            continue
        holdings[code] = Holding(
            code=code,
            name=clean_text(item.get("name")) or code,
            account=clean_text(item.get("account")) or "",
            type=REPORTABLE_TYPE,
        )

    if holdings:
        print(
            "WARNING: using current_stock_list.json for current holding identity; "
            "full holdings_snapshot.json is still required for account pages.",
            file=sys.stderr,
        )
    return holdings


def current_stock_holdings(snapshot: dict) -> dict[str, Holding]:
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(accounts, dict):
        return {}

    holdings: dict[str, Holding] = {}
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
            holdings[code] = Holding(
                code=code,
                name=clean_text(item.get("name")) or code,
                account=clean_text(item.get("account")) or clean_text(account),
                type=REPORTABLE_TYPE,
            )
    return holdings


def load_current_stock_holdings() -> dict[str, Holding]:
    snapshot = load_snapshot()
    holdings = current_stock_holdings(snapshot)
    if holdings:
        return holdings
    return load_current_stock_list()


def latest_stock_report(reports_dir: Path = REPORTS_DIR) -> Path | None:
    reports = [p for p in reports_dir.glob("report_20*.md") if p.is_file()]
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
    markdown_text = report_path.read_text(encoding="utf-8", errors="ignore")
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
            "account": holding.account,
            "action": action,
            "score": score,
            "sentiment": sentiment,
            "summary": summary[:240],
            "source_report": f"reports/{report_path.name}",
            "holding_snapshot_date": snapshot_date,
            "is_current_holding_when_advised": True,
            "advice_close": None,
            "created_at": now_iso(),
        }

    if not records:
        print(f"WARNING: no stock advice extracted from {report_path}", file=sys.stderr)
    return list(records.values())


def read_history_file(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"WARNING: skip invalid history line {path}:{line_number}: {exc}", file=sys.stderr)
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def fetch_pages_history(url: str = PAGES_HISTORY_URL) -> list[dict[str, Any]]:
    request = Request(url, headers={"User-Agent": "daily-stock-analysis-advice-backtest"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read().decode("utf-8", "ignore")
    except Exception as exc:
        print(f"WARNING: cannot fetch previous Pages advice history: {exc}", file=sys.stderr)
        return []

    records: list[dict[str, Any]] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def load_history() -> list[dict[str, Any]]:
    local_records: list[dict[str, Any]] = []
    for path in (LOCAL_HISTORY_PATH, SITE_DATA_HISTORY_PATH):
        local_records.extend(read_history_file(path))
    if local_records:
        return merge_history(local_records)
    remote_records = fetch_pages_history()
    return merge_history(remote_records)


def sanitize_history_record(record: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in record.items() if key not in SENSITIVE_KEYS}
    cleaned["schema_version"] = int(cleaned.get("schema_version") or SCHEMA_VERSION)
    cleaned["code"] = normalize_code(cleaned.get("code"))
    cleaned["type"] = clean_text(cleaned.get("type")) or REPORTABLE_TYPE
    cleaned["name"] = clean_text(cleaned.get("name")) or cleaned["code"]
    cleaned["account"] = clean_text(cleaned.get("account"))
    cleaned["action"] = clean_text(cleaned.get("action")) or "unknown"
    cleaned["sentiment"] = clean_text(cleaned.get("sentiment")) or "unknown"
    cleaned["summary"] = clean_text(cleaned.get("summary"))[:240]
    return cleaned


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
        cleaned["date"] = date_text
        cleaned["code"] = code
        merged[(date_text, code)] = cleaned
    return sorted(merged.values(), key=lambda item: (item.get("date", ""), item.get("code", "")))


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
    text = f"{record.get('action', '')} {record.get('sentiment', '')}"
    if any(token in text for token in ("买入", "加仓", "看多", "强烈看多", "偏多")):
        return "买入类"
    if any(token in text for token in ("卖出", "减仓", "避险", "看空", "强烈看空", "偏空")):
        return "卖出类"
    if any(token in text for token in ("持有", "观望", "中性", "震荡", "等待")):
        return "持有/观望类"
    return "unknown"


def evaluate_hit(group: str, period: str, return_value: float) -> tuple[bool | None, str]:
    if group == "买入类":
        return return_value > 0, "买入后下跌" if return_value <= 0 else ""
    if group == "卖出类":
        return return_value < 0, "卖出后上涨" if return_value >= 0 else ""
    if group == "持有/观望类":
        band = HOLD_BAND[period]
        if -band <= return_value <= band:
            return True, ""
        return False, "观望后大涨" if return_value > band else "观望后大跌"
    return None, "unknown"


def available_weekday_count_after(analysis_date: date, today: date) -> int:
    if today <= analysis_date:
        return 0
    count = 0
    current = analysis_date + timedelta(days=1)
    while current <= today:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def is_validation_window_pending(analysis_date: date, offset: int) -> bool:
    """Return True when the T+N window has not plausibly arrived yet.

    The exact T+N target is based on effective trading bars. When the market
    data source has no bars yet, this weekday lower bound keeps same-day,
    weekend, and near-future advice from being reported as missing data.
    """

    today = datetime.now(SHANGHAI_TZ).date()
    return available_weekday_count_after(analysis_date, today) < offset


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


def evaluate_record(record: dict[str, Any], provider: PriceProvider) -> dict[str, Any]:
    result = dict(record)
    result.pop("price_warning", None)
    analysis_date = parse_date(result.get("date"))
    group = classify_advice(result)
    result["action_group"] = group
    if analysis_date is None:
        for period in PERIODS:
            result[f"{period}_status"] = result.get(f"{period}_status") or "数据不足"
            result[f"{period}_hit"] = None
        return result

    if all(
        result.get(f"{period}_status") == "已验证"
        and parse_float(result.get(f"{period}_close")) is not None
        for period in PERIODS
    ):
        return result

    code = normalize_code(result.get("code"))
    bars, error = provider.get_bars(code, analysis_date)
    bars = sorted({bar.trade_date: bar for bar in bars if bar.close > 0}.values(), key=lambda bar: bar.trade_date)
    if not bars:
        result["price_warning"] = price_diagnostic(
            code=code,
            analysis_date=analysis_date,
            period=None,
            missing="advice_close",
            bars=bars,
            error=error,
        )
        for period in PERIODS:
            if result.get(f"{period}_status") == "已验证":
                continue
            result[f"{period}_status"] = "等待验证" if is_validation_window_pending(analysis_date, PERIODS[period]) else "数据不足"
            result[f"{period}_hit"] = None
            result.setdefault(f"{period}_close", None)
            result.setdefault(f"{period}_return", None)
        return result

    advice_index: int | None = None
    advice_bar: DailyBar | None = None
    for index, bar in enumerate(bars):
        if bar.trade_date <= analysis_date:
            advice_index = index
            advice_bar = bar
        else:
            break

    advice_close = parse_float(result.get("advice_close"))
    if (advice_close is None or advice_close <= 0) and advice_bar is not None:
        advice_close = advice_bar.close

    if advice_bar is None or advice_index is None or advice_close is None or advice_close <= 0:
        result["price_warning"] = price_diagnostic(
            code=code,
            analysis_date=analysis_date,
            period=None,
            missing="advice_close",
            bars=bars,
            error=error,
        )
        for period in PERIODS:
            if result.get(f"{period}_status") == "已验证":
                continue
            result[f"{period}_status"] = "等待验证" if is_validation_window_pending(analysis_date, PERIODS[period]) else "数据不足"
            result[f"{period}_hit"] = None
        return result

    result["advice_close"] = round(advice_close, 4)
    result["advice_close_date"] = advice_bar.trade_date.isoformat()
    forward = bars[advice_index + 1 :]
    for period, offset in PERIODS.items():
        if result.get(f"{period}_status") == "已验证" and parse_float(result.get(f"{period}_close")) is not None:
            continue
        if len(forward) < offset:
            pending = is_validation_window_pending(advice_bar.trade_date, offset)
            if not pending:
                result["price_warning"] = price_diagnostic(
                    code=code,
                    analysis_date=analysis_date,
                    period=period,
                    missing="target_close",
                    bars=bars,
                    error=error,
                    advice_close_date=advice_bar.trade_date,
                )
            result[f"{period}_status"] = "等待验证" if pending else "数据不足"
            result[f"{period}_hit"] = None
            result.setdefault(f"{period}_close", None)
            result.setdefault(f"{period}_return", None)
            continue

        target = forward[offset - 1]
        return_value = (target.close - advice_close) / advice_close
        hit, miss_reason = evaluate_hit(group, period, return_value)
        result[f"{period}_status"] = "已验证"
        result[f"{period}_close"] = round(target.close, 4)
        result[f"{period}_date"] = target.trade_date.isoformat()
        result[f"{period}_return"] = round(return_value, 6)
        result[f"{period}_hit"] = hit
        if miss_reason:
            result[f"{period}_miss_reason"] = miss_reason
    return result


def evaluate_records(
    history: list[dict[str, Any]],
    provider: PriceProvider,
    current_codes: set[str],
) -> list[dict[str, Any]]:
    evaluated = []
    ordered_history = sorted(
        history,
        key=lambda item: (str(item.get("date") or ""), normalize_code(item.get("code"))),
    )
    for record in ordered_history:
        item = evaluate_record(record, provider)
        item["is_current_holding_now"] = item.get("code") in current_codes
        evaluated.append(item)
    return sorted(evaluated, key=lambda item: (item.get("date", ""), item.get("code", "")))


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"total_advice": len(records)}
    for period in PERIODS:
        evaluated = [r for r in records if r.get(f"{period}_status") == "已验证" and isinstance(r.get(f"{period}_hit"), bool)]
        hit_count = sum(1 for r in evaluated if r.get(f"{period}_hit") is True)
        waiting = sum(1 for r in records if r.get(f"{period}_status") == "等待验证")
        insufficient = sum(1 for r in records if r.get(f"{period}_status") == "数据不足")
        summary[f"{period}_evaluated"] = len(evaluated)
        summary[f"{period}_hit"] = hit_count
        summary[f"{period}_waiting"] = waiting
        summary[f"{period}_insufficient"] = insufficient
        summary[f"{period}_hit_rate"] = round(hit_count / len(evaluated), 4) if evaluated else None
    return summary


def group_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {"买入类": [], "卖出类": [], "持有/观望类": [], "unknown": []}
    for record in records:
        groups.setdefault(str(record.get("action_group") or "unknown"), []).append(record)
    return {group: summarize_records(items) for group, items in groups.items()}


def build_accuracy(records: list[dict[str, Any]]) -> dict[str, Any]:
    current_records = [record for record in records if record.get("is_current_holding_now")]
    miss_cases = []
    for record in sorted(records, key=lambda item: item.get("date", ""), reverse=True):
        for period in PERIODS:
            if record.get(f"{period}_hit") is False:
                item = {
                    "date": record.get("date"),
                    "code": record.get("code"),
                    "name": record.get("name"),
                    "account": record.get("account"),
                    "action": record.get("action"),
                    "sentiment": record.get("sentiment"),
                    "period": period,
                    "return": record.get(f"{period}_return"),
                    "miss_reason": record.get(f"{period}_miss_reason") or "未命中",
                    "is_current_holding_now": record.get("is_current_holding_now"),
                }
                miss_cases.append(item)
                break

    return {
        "updated_at": now_iso(),
        "summary_all_history": summarize_records(records),
        "summary_current_holdings": summarize_records(current_records),
        "by_action_all_history": group_stats(records),
        "by_action_current_holdings": group_stats(current_records),
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
    hit = record.get(f"{period}_hit")
    if status != "已验证":
        return status
    if hit is True:
        return "命中"
    if hit is False:
        return str(record.get(f"{period}_miss_reason") or "未命中")
    return "样本不足"


def render_summary_card(title: str, summary: dict[str, Any]) -> str:
    return f"""
<article class="metric-card">
  <h3>{escape(title)}</h3>
  <p>已记录建议数量：<strong>{summary.get('total_advice', 0)}</strong></p>
  <div class="metric-grid">
    <div><span>T+1 已验证</span><strong>{summary.get('d1_evaluated', 0)}</strong><em>{format_rate(summary.get('d1_hit_rate'))}</em></div>
    <div><span>T+5 已验证</span><strong>{summary.get('d5_evaluated', 0)}</strong><em>{format_rate(summary.get('d5_hit_rate'))}</em></div>
    <div><span>T+20 已验证</span><strong>{summary.get('d20_evaluated', 0)}</strong><em>{format_rate(summary.get('d20_hit_rate'))}</em></div>
  </div>
</article>
"""


def render_record_card(record: dict[str, Any]) -> str:
    current = "当前仍持有" if record.get("is_current_holding_now") else "已不在当前持仓"
    score = record.get("score")
    score_text = "unknown" if score is None else str(score)
    price_warning = ""
    if record.get("price_warning"):
        price_warning = (
            '<details class="diagnostic-details"><summary>价格诊断</summary>'
            f'<p>{escape(str(record.get("price_warning")))}</p></details>'
        )
    return f"""
<article class="record-card">
  <div class="record-head">
    <div><h4>{escape(str(record.get('name') or ''))}</h4><span class="record-code">{escape(str(record.get('code') or ''))}</span></div>
    <span class="holding-state">{current}</span>
  </div>
  <p class="record-meta">{escape(str(record.get('date') or ''))} · {escape(str(record.get('account') or ''))}</p>
  <p class="advice-line"><strong>{escape(str(record.get('action') or 'unknown'))}</strong><span>评分 {escape(score_text)}</span><span>{escape(str(record.get('sentiment') or 'unknown'))}</span></p>
  <div class="period-grid">
    <span><small>T+1</small><strong>{escape(status_text(record, 'd1'))}</strong><em>{escape(format_return(record.get('d1_return')))}</em></span>
    <span><small>T+5</small><strong>{escape(status_text(record, 'd5'))}</strong><em>{escape(format_return(record.get('d5_return')))}</em></span>
    <span><small>T+20</small><strong>{escape(status_text(record, 'd20'))}</strong><em>{escape(format_return(record.get('d20_return')))}</em></span>
  </div>
  {price_warning}
</article>
"""


def render_period_stats(summary: dict[str, Any]) -> str:
    labels = {"d1": "日维度 T+1", "d5": "周维度 T+5", "d20": "月维度 T+20"}
    cards = []
    for period, label in labels.items():
        cards.append(
            f"""
<article class="small-card">
  <h4>{label}</h4>
  <p>已验证样本数：{summary.get(f'{period}_evaluated', 0)}</p>
  <p>命中数：{summary.get(f'{period}_hit', 0)}</p>
  <p>命中率：{format_rate(summary.get(f'{period}_hit_rate'))}</p>
  <p>等待验证：{summary.get(f'{period}_waiting', 0)}</p>
  <p>数据不足：{summary.get(f'{period}_insufficient', 0)}</p>
</article>
"""
        )
    return "".join(cards)


def render_action_stats(by_action: dict[str, Any]) -> str:
    cards = []
    for group, summary in by_action.items():
        cards.append(
            f"""
<article class="small-card">
  <h4>{escape(group)}</h4>
  <p>样本数：{summary.get('total_advice', 0)}</p>
  <p>T+1：{format_rate(summary.get('d1_hit_rate'))}</p>
  <p>T+5：{format_rate(summary.get('d5_hit_rate'))}</p>
  <p>T+20：{format_rate(summary.get('d20_hit_rate'))}</p>
</article>
"""
        )
    return "".join(cards)


def _latest_record_per_code(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        code = str(record.get("code") or "")
        previous = latest.get(code)
        if previous is None or str(record.get("date") or "") >= str(previous.get("date") or ""):
            latest[code] = record
    return sorted(latest.values(), key=lambda item: (str(item.get("date") or ""), str(item.get("code") or "")), reverse=True)


def render_html(accuracy: dict[str, Any]) -> str:
    records = list(accuracy.get("records", []))
    current_records = [record for record in records if record.get("is_current_holding_now")]
    latest_current_records = _latest_record_per_code(current_records)
    recent_records = list(accuracy.get("recent_records", []))
    miss_cases = list(accuracy.get("miss_cases", []))

    current_latest_html = "".join(render_record_card(record) for record in latest_current_records) or '<p class="muted">暂无当前持仓建议样本。</p>'
    current_history_html = "".join(render_record_card(record) for record in current_records[::-1])
    all_html = "".join(render_record_card(record) for record in records[::-1]) or '<p class="muted">暂无历史建议样本。</p>'
    recent_html = "".join(render_record_card(record) for record in recent_records) or '<p class="muted">暂无最近建议。</p>'
    miss_html = "".join(render_record_card(record) for record in miss_cases) or '<p class="muted">暂无已验证未命中样本。</p>'
    current_history_details = ""
    if current_history_html:
        current_history_details = f"""
<details class="collection-details">
  <summary><span>查看当前持仓全部历史建议</span><span class="summary-count">{len(current_records)} 条</span></summary>
  <div class="collection-body"><div class="record-grid">{current_history_html}</div></div>
</details>
"""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI 建议准确性回测</title>
  <style>
    :root {{ color-scheme:light; --border:#dbe3ed; --border-strong:#c8d3e0; --muted:#637086; --bg:#f3f6fa; --card:#fff; --text:#182235; --accent:#075fca; --soft:#f7f9fc; }}
    * {{ box-sizing:border-box; }}
    html {{ min-width:0; background:var(--bg); }}
    body {{ min-width:0; margin:0; color:var(--text); background:var(--bg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; line-height:1.68; overflow-wrap:anywhere; overflow-x:hidden; }}
    main {{ width:100%; max-width:1120px; min-width:0; margin:0 auto; padding:24px 20px 48px; }}
    a {{ color:var(--accent); text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    .page-nav {{ margin-bottom:16px; padding:0 2px 12px; border-bottom:1px solid var(--border); font-size:14px; }}
    .hero {{ padding:24px; margin:0 0 20px; background:var(--card); border:1px solid var(--border); border-radius:8px; box-shadow:0 8px 24px rgba(24,34,53,.05); }}
    .hero-kicker {{ display:block; margin-bottom:6px; color:var(--accent); font-size:13px; font-weight:700; }}
    .hero h1 {{ margin:0; font-size:30px; line-height:1.28; }}
    .hero-copy {{ max-width:760px; margin:10px 0 0; color:var(--muted); }}
    .meta-row {{ display:flex; flex-wrap:wrap; gap:8px 18px; margin-top:14px; color:var(--muted); }}
    .overview-grid,.card-grid,.record-grid {{ display:grid; gap:12px; min-width:0; }}
    .overview-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
    .card-grid {{ grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr)); }}
    .record-grid {{ grid-template-columns:repeat(auto-fit,minmax(min(100%,330px),1fr)); }}
    .panel,.metric-card,.record-card,.small-card {{ min-width:0; background:var(--card); border:1px solid var(--border); border-radius:8px; }}
    .panel {{ padding:20px; margin:0 0 16px; }}
    .metric-card,.small-card {{ padding:18px; margin:0; }}
    .record-card {{ padding:16px; margin:0; }}
    h2 {{ margin:0 0 4px; font-size:21px; }}
    h3,h4 {{ margin:0; }}
    .section-intro {{ margin:0 0 14px; color:var(--muted); font-size:14px; }}
    .muted,.record-meta {{ color:var(--muted); }}
    .metric-card > p {{ margin:8px 0 12px; }}
    .metric-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }}
    .metric-grid div {{ min-width:0; padding:11px; background:var(--soft); border:1px solid #e7ecf3; border-radius:6px; }}
    .metric-grid span,.metric-grid em {{ display:block; color:var(--muted); font-size:13px; font-style:normal; }}
    .metric-grid strong {{ display:block; margin:3px 0; font-size:23px; font-variant-numeric:tabular-nums; }}
    .small-card p {{ margin:4px 0; }}
    .record-head {{ display:flex; align-items:start; justify-content:space-between; gap:10px; }}
    .record-head h4 {{ display:inline; font-size:17px; }}
    .record-code {{ margin-left:7px; color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; }}
    .holding-state {{ flex:0 0 auto; padding:3px 8px; color:#075fca; background:#eaf2ff; border-radius:999px; font-size:12px; font-weight:650; }}
    .record-meta {{ margin:7px 0; font-size:13px; }}
    .advice-line {{ display:flex; flex-wrap:wrap; gap:6px 10px; margin:9px 0; }}
    .advice-line strong {{ color:var(--accent); }}
    .period-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; }}
    .period-grid > span {{ min-width:0; padding:8px; background:var(--soft); border:1px solid #e7ecf3; border-radius:6px; }}
    .period-grid small,.period-grid strong,.period-grid em {{ display:block; min-width:0; }}
    .period-grid small,.period-grid em {{ color:var(--muted); font-size:12px; font-style:normal; }}
    .period-grid strong {{ margin:2px 0; font-size:13px; }}
    .diagnostic-details {{ margin-top:10px; border-top:1px solid var(--border); }}
    .diagnostic-details summary {{ padding-top:9px; color:var(--muted); cursor:pointer; font-size:13px; }}
    .diagnostic-details p {{ margin:7px 0 0; color:var(--muted); font-size:13px; }}
    .collection-details {{ margin:14px 0 0; border:1px solid var(--border); border-radius:8px; background:var(--card); overflow:hidden; }}
    .collection-details > summary {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 14px; cursor:pointer; background:var(--soft); font-weight:700; }}
    .summary-count {{ color:var(--muted); font-size:13px; font-weight:600; }}
    .collection-body {{ padding:14px; }}
    footer {{ margin-top:24px; padding-top:16px; color:var(--muted); border-top:1px solid var(--border); font-size:14px; }}
    @media (max-width:640px) {{
      main {{ padding:12px 12px 36px; }}
      .hero {{ padding:18px; }}
      .hero h1 {{ font-size:25px; }}
      .overview-grid,.card-grid,.record-grid,.metric-grid,.period-grid {{ grid-template-columns:1fr; }}
      .panel,.metric-card,.small-card,.record-card {{ padding:14px; }}
      .collection-body {{ padding:10px; }}
      .record-head {{ display:block; }}
      .holding-state {{ display:inline-flex; margin-top:7px; }}
    }}
  </style>
</head>
<body>
<main>
  <nav class="page-nav"><a href="index.html">返回首页</a></nav>
  <header class="hero">
    <span class="hero-kicker">历史建议规则回测</span>
    <h1>AI 建议准确性回测</h1>
    <p class="hero-copy">只使用已生成建议、当前持仓快照与后续真实行情，不调用 Gemini 或任何 LLM。</p>
    <div class="meta-row">
      <span>更新时间：{escape(str(accuracy.get('updated_at') or now_iso()))}</span>
      <span>最新读取日报：{escape(str(accuracy.get('latest_report_date') or '暂无'))}</span>
      <span>{escape(str(accuracy.get('new_advice_message') or '本次无新增可回测建议。'))}</span>
    </div>
  </header>

  <section class="overview-grid">
    {render_summary_card("全部历史建议", accuracy.get("summary_all_history", {}))}
    {render_summary_card("当前持仓建议", accuracy.get("summary_current_holdings", {}))}
  </section>

  <section class="panel">
    <h2>分周期统计</h2>
    <p class="section-intro">分别观察日、周、月三个验证窗口。</p>
    <div class="card-grid">{render_period_stats(accuracy.get("summary_all_history", {}))}</div>
  </section>

  <section class="panel">
    <h2>按建议类型统计</h2>
    <p class="section-intro">买入类、卖出类与持有观望类使用各自可解释的命中规则。</p>
    <div class="card-grid">{render_action_stats(accuracy.get("by_action_all_history", {}))}</div>
  </section>

  <section class="panel">
    <h2>当前持仓建议回看</h2>
    <p class="section-intro">默认展示每只当前持仓最近一条建议；完整历史仍保留在折叠区。</p>
    <div class="record-grid">{current_latest_html}</div>
    {current_history_details}
  </section>

  <details class="collection-details">
    <summary><span>历史全部建议回测</span><span class="summary-count">{len(records)} 条</span></summary>
    <div class="collection-body"><div class="record-grid">{all_html}</div></div>
  </details>

  <section class="panel">
    <h2>最近建议回看</h2>
    <p class="section-intro">最近 20 条建议及其验证进度。</p>
    <div class="record-grid">{recent_html}</div>
  </section>

  <details class="collection-details">
    <summary><span>最近错误案例</span><span class="summary-count">{len(miss_cases)} 条</span></summary>
    <div class="collection-body"><div class="record-grid">{miss_html}</div></div>
  </details>

  <section class="panel">
    <h2>数据状态说明</h2>
    <p>后续第 N 个有效交易日尚未出现时显示“等待验证”；行情源无法返回建议日或目标交易日收盘价时显示“数据不足”。样本不足时不显示误导性的 0% 命中率。</p>
  </section>

  <footer>{escape(DISCLAIMER)}</footer>
</main>
</body>
</html>
"""


def markdown_summary_line(summary: dict[str, Any], label: str) -> list[str]:
    return [
        f"### {label}",
        "",
        f"- 已记录建议数量：{summary.get('total_advice', 0)}",
        f"- T+1 已验证：{summary.get('d1_evaluated', 0)}，命中率：{format_rate(summary.get('d1_hit_rate'))}",
        f"- T+5 已验证：{summary.get('d5_evaluated', 0)}，命中率：{format_rate(summary.get('d5_hit_rate'))}",
        f"- T+20 已验证：{summary.get('d20_evaluated', 0)}，命中率：{format_rate(summary.get('d20_hit_rate'))}",
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

    lines.extend(["## 按建议类型统计", ""])
    for group, summary in accuracy.get("by_action_all_history", {}).items():
        lines.append(f"- {group}：样本 {summary.get('total_advice', 0)}，T+1 {format_rate(summary.get('d1_hit_rate'))}，T+5 {format_rate(summary.get('d5_hit_rate'))}，T+20 {format_rate(summary.get('d20_hit_rate'))}")
    lines.append("")

    lines.extend(["## 当前持仓建议回看", ""])
    current = [record for record in records if record.get("is_current_holding_now")]
    if not current:
        lines.append("暂无当前持仓建议样本。")
    for record in current:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action')}，T+1 {status_text(record, 'd1')}，T+5 {status_text(record, 'd5')}，T+20 {status_text(record, 'd20')}")
    lines.append("")

    lines.extend(["## 最近建议回看", ""])
    recent_records = list(accuracy.get("recent_records", []))
    if not recent_records:
        lines.append("暂无最近建议样本。")
    for record in recent_records:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action')} / {record.get('sentiment')}，T+1 {status_text(record, 'd1')}，T+5 {status_text(record, 'd5')}，T+20 {status_text(record, 'd20')}")
    lines.append("")

    lines.extend(["## 历史全部建议回测", ""])
    if not records:
        lines.append("暂无历史建议样本。")
    for record in records:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action')} / {record.get('sentiment')}，T+1 {status_text(record, 'd1')}，T+5 {status_text(record, 'd5')}，T+20 {status_text(record, 'd20')}")
    lines.append("")

    lines.extend(["## 最近错误案例", ""])
    miss_cases = accuracy.get("miss_cases", [])
    if not miss_cases:
        lines.append("暂无已验证未命中样本。")
    for record in miss_cases[:20]:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action')}，{record.get('period')} {record.get('miss_reason')}，收益率 {format_return(record.get('return'))}")
    lines.append("")

    lines.extend([
        "## 数据不足说明",
        "",
        "- T+1 / T+5 / T+20 使用建议日之后第 N 个有收盘价的交易日。",
        "- 尚未到达验证窗口时显示“等待验证”。",
        "- 行情数据源缺少建议日或后续收盘价时显示“数据不足”。",
        "",
        DISCLAIMER,
        "",
    ])
    return "\n".join(lines)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_index_entry() -> None:
    index_path = SITE_DIR / "index.html"
    link_html = '<li>AI 建议准确性回测：<a href="advice_backtest.html">AI 建议准确性回测</a></li>'
    if not index_path.exists():
        SITE_DIR.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            f"<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>每日持仓复盘</title><body><main><h1>每日持仓复盘</h1><ul>{link_html}</ul></main></body></html>",
            encoding="utf-8",
        )
        return

    html = index_path.read_text(encoding="utf-8", errors="ignore")
    if "advice_backtest.html" in html:
        return
    marker = "</ul>"
    if marker in html:
        html = html.replace(marker, f"{link_html}{marker}", 1)
    else:
        html = html.replace("</main>", f"<section class=\"panel\"><h2>AI 建议准确性回测</h2><ul>{link_html}</ul></section></main>", 1)
    index_path.write_text(html, encoding="utf-8")


def write_outputs(history: list[dict[str, Any]], accuracy: dict[str, Any], report_date: str) -> None:
    write_jsonl(LOCAL_HISTORY_PATH, history)
    write_jsonl(SITE_DATA_HISTORY_PATH, history)
    write_json(LOCAL_ACCURACY_PATH, accuracy)
    write_json(SITE_DATA_ACCURACY_PATH, accuracy)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    markdown_path = REPORTS_DIR / f"advice_accuracy_{report_date.replace('-', '')}.md"
    markdown_path.write_text(render_markdown(accuracy, report_date), encoding="utf-8")

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "advice_backtest.html").write_text(render_html(accuracy), encoding="utf-8")
    ensure_index_entry()

    print(f"Advice history written: {LOCAL_HISTORY_PATH.relative_to(ROOT_DIR)}")
    print(f"Advice accuracy written: {LOCAL_ACCURACY_PATH.relative_to(ROOT_DIR)}")
    print(f"Advice backtest page written: {(SITE_DIR / 'advice_backtest.html').relative_to(ROOT_DIR)}")
    print(f"Advice markdown report written: {markdown_path.relative_to(ROOT_DIR)}")


def make_test_bars(base_date: date, base_close: float, moves: list[float]) -> list[DailyBar]:
    bars = [DailyBar(base_date, base_close)]
    current = base_date
    for move in moves:
        current += timedelta(days=1)
        bars.append(DailyBar(current, round(base_close * (1 + move), 4)))
    return bars


def setup_test_fixture() -> MockPriceProvider:
    for path in (REPORTS_DIR, DATA_DIR, SITE_DIR, ROOT_DIR / "site_data"):
        path.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "generated_at": "2099-01-10 18:00:00",
        "source_url": "test",
        "accounts": {
            "测试账户A": {
                "stock": [
                    {"account": "测试账户A", "type": "stock", "name": "买入上涨", "code": "111111"},
                    {"account": "测试账户A", "type": "stock", "name": "新买入股票", "code": "999999"},
                ],
                "lof": [{"account": "测试账户A", "type": "lof", "name": "测试ETF", "code": "333333"}],
                "otc": [],
            },
            "测试账户B": {
                "stock": [
                    {"account": "测试账户B", "type": "stock", "name": "卖出下跌", "code": "222222"},
                    {"account": "测试账户B", "type": "stock", "name": "等待验证", "code": "777777"},
                    {"account": "测试账户B", "type": "stock", "name": "数据不足", "code": "888888"},
                    {"account": "测试账户B", "type": "stock", "name": "分析失败", "code": "101010"},
                ],
                "lof": [],
                "otc": [{"account": "测试账户B", "type": "otc", "name": "测试场外基金", "code": "121212"}],
            },
        },
    }
    (ROOT_DIR / "site_data" / "holdings_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    old_history = [
        {"schema_version": 1, "date": "2099-01-01", "code": "444444", "name": "已卖出买入后下跌", "type": "stock", "account": "旧账户", "action": "买入", "score": 80, "sentiment": "看多", "summary": "买入后下跌", "source_report": "reports/report_20990101.md", "holding_snapshot_date": "2099-01-01", "is_current_holding_when_advised": True, "created_at": now_iso()},
        {"schema_version": 1, "date": "2099-01-01", "code": "555555", "name": "卖出后上涨", "type": "stock", "account": "旧账户", "action": "卖出", "score": 20, "sentiment": "看空", "summary": "卖出后上涨", "source_report": "reports/report_20990101.md", "holding_snapshot_date": "2099-01-01", "is_current_holding_when_advised": True, "created_at": now_iso()},
        {"schema_version": 1, "date": "2099-01-01", "code": "666666", "name": "观望后震荡", "type": "stock", "account": "旧账户", "action": "观望", "score": 50, "sentiment": "震荡", "summary": "观望后震荡", "source_report": "reports/report_20990101.md", "holding_snapshot_date": "2099-01-01", "is_current_holding_when_advised": True, "created_at": now_iso()},
        {"schema_version": 1, "date": "2099-01-02", "code": "666666", "name": "观望后大涨", "type": "stock", "account": "旧账户", "action": "观望", "score": 50, "sentiment": "震荡", "summary": "观望后大涨", "source_report": "reports/report_20990102.md", "holding_snapshot_date": "2099-01-02", "is_current_holding_when_advised": True, "created_at": now_iso()},
    ]
    write_jsonl(LOCAL_HISTORY_PATH, old_history)

    report = """# 2099-01-10 股票日报

## 分析结果摘要

- 买入上涨（111111） A股个股：买入｜评分 88｜强烈看多
- 卖出下跌(222222)：卖出 | 评分 20 | 强烈看空
- 新买入股票（999999） A股个股：持有｜评分 55｜震荡
- 等待验证（777777） A股个股：观望｜评分 50｜中性
- 数据不足（888888） A股个股：买入｜评分 70｜看多
- 分析失败（101010） A股个股：分析失败：Gemini 模型服务暂不可用，本标的未完成分析。
- 测试ETF（333333） LOF/ETF：已纳入账户级组合复盘

## LOF/ETF 组合复盘

### 测试账户A

#### 组合观察

- 这部分不能进入建议回测。
"""
    (REPORTS_DIR / "report_20990110.md").write_text(report, encoding="utf-8")
    (SITE_DIR / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body><main><h1>每日持仓复盘</h1><ul></ul></main></body></html>',
        encoding="utf-8",
    )

    base = date(2099, 1, 10)
    old = date(2099, 1, 1)
    bars = {
        "111111": make_test_bars(base, 10.0, [0.02, 0.01, 0.03, 0.04, 0.06, 0.08, 0.09, 0.1, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19, 0.2, 0.21, 0.22]),
        "222222": make_test_bars(base, 10.0, [-0.02, -0.03, -0.04, -0.05, -0.06, -0.07, -0.08, -0.09, -0.1, -0.11, -0.12, -0.13, -0.14, -0.15, -0.16, -0.17, -0.18, -0.19, -0.2, -0.21]),
        "999999": make_test_bars(base, 10.0, [0.01, 0.02, 0.01, -0.01, 0.0]),
        "777777": make_test_bars(base, 10.0, [0.01]),
        "444444": make_test_bars(old, 10.0, [-0.02, -0.02, -0.02, -0.03, -0.04, -0.05, -0.06, -0.07, -0.08, -0.09, -0.1, -0.11, -0.12, -0.13, -0.14, -0.15, -0.16, -0.17, -0.18, -0.19]),
        "555555": make_test_bars(old, 10.0, [0.02, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19, 0.2]),
        "666666": make_test_bars(old, 10.0, [0.01, 0.02, -0.01, 0.0, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17]),
    }
    return MockPriceProvider(bars)


def run_backtest(provider: PriceProvider | None = None) -> dict[str, Any]:
    holdings = load_current_stock_holdings()
    report_path = latest_stock_report()
    new_records = extract_advice_from_report(report_path, holdings) if report_path else []
    history = merge_history([*load_history(), *new_records])
    current_codes = set(holdings)
    price_provider = provider or DataFetcherPriceProvider()
    evaluated = evaluate_records(history, price_provider, current_codes)
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
        new_advice_count=len(new_records),
    )
    write_outputs(evaluated, accuracy, report_date)
    return accuracy


def assert_test_results(accuracy: dict[str, Any]) -> None:
    records = accuracy.get("records", [])
    codes = {record.get("code") for record in records}
    hold_observe = parse_advice_line("贵研铂业（600459） A股个股：持有观察｜评分 59｜强烈看多")
    assert hold_observe and hold_observe[0] == "600459" and hold_observe[2] == "持有观察"
    watch = parse_advice_line("株冶集团（600961） A股个股：观望｜评分 45｜震荡")
    assert watch and watch[0] == "600961" and watch[2] == "观望"
    assert "333333" not in codes, "LOF/ETF must not enter advice backtest"
    assert "121212" not in codes, "OTC must not enter advice backtest"
    assert "101010" not in codes, "failed stock analysis must not enter advice backtest"
    assert "999999" in codes, "new stock holding should enter advice history"
    assert "444444" in codes, "sold stock historical advice should remain"
    assert len([r for r in records if r.get("code") == "666666"]) == 2, "different dates for same code must be kept"
    assert any(r.get("code") == "777777" and r.get("d5_status") == "等待验证" for r in records)
    assert any(r.get("code") == "888888" and r.get("d1_status") == "等待验证" for r in records)
    assert accuracy["summary_all_history"]["total_advice"] >= 9
    assert "买入类" in accuracy["by_action_all_history"]
    assert accuracy["recent_records"]
    assert accuracy["miss_cases"]
    assert (SITE_DIR / "advice_backtest.html").exists()
    assert (REPORTS_DIR / "advice_accuracy_20990110.md").exists()
    assert (SITE_DIR / "index.html").read_text(encoding="utf-8").find("advice_backtest.html") != -1
    assert "2099-01-10" in (SITE_DIR / "advice_backtest.html").read_text(encoding="utf-8")

    today = datetime.now(SHANGHAI_TZ).date()
    fresh = evaluate_record(
        {"date": today.isoformat(), "code": "101010", "action": "买入", "sentiment": "看多"},
        MockErrorPriceProvider(),
    )
    assert all(fresh.get(f"{period}_status") == "等待验证" for period in PERIODS), "same-day advice must wait for validation"

    old_date = today - timedelta(days=10)
    old_missing = evaluate_record(
        {"date": old_date.isoformat(), "code": "202020", "action": "买入", "sentiment": "看多"},
        MockErrorPriceProvider(),
    )
    assert old_missing.get("d1_status") == "数据不足", "past advice with missing market data must be insufficient"

    old_verified = evaluate_record(
        {"date": old_date.isoformat(), "code": "303030", "action": "买入", "sentiment": "看多"},
        MockPriceProvider({"303030": make_test_bars(old_date, 10.0, [0.01])}),
    )
    assert old_verified.get("d1_status") == "已验证", "past advice with target close must be evaluated"
    assert old_verified.get("advice_close") == 10.0, "missing advice_close must be backfilled from historical bars"
    assert old_verified.get("d1_close") == 10.1
    assert old_verified.get("advice_close_date") == old_date.isoformat()

    june18 = date(2026, 6, 18)
    june18_verified = evaluate_record(
        {"date": june18.isoformat(), "code": "600961", "action": "观望", "sentiment": "震荡"},
        MockPriceProvider({"600961": [DailyBar(june18, 10.0), DailyBar(date(2026, 6, 19), 10.2)]}),
    )
    assert june18_verified.get("d1_status") == "已验证"
    assert june18_verified.get("advice_close_date") == "2026-06-18"
    assert june18_verified.get("d1_date") == "2026-06-19"
    assert june18_verified.get("d1_close") == 10.2

    days_until_sunday = (6 - today.weekday()) % 7 or 7
    weekend_date = today + timedelta(days=days_until_sunday)
    prior_friday = weekend_date - timedelta(days=2)
    weekend_waiting = evaluate_record(
        {"date": weekend_date.isoformat(), "code": "600961", "action": "观望", "sentiment": "震荡"},
        MockPriceProvider({"600961": [DailyBar(prior_friday, 10.0)]}),
    )
    assert weekend_waiting.get("advice_close_date") == prior_friday.isoformat()
    assert weekend_waiting.get("d1_status") == "等待验证"

    assert "sh.600961" in DataFetcherPriceProvider._code_variants("600961")
    assert "sz.000651" in DataFetcherPriceProvider._code_variants("000651")

    missing_start = evaluate_record(
        {"date": old_date.isoformat(), "code": "505050", "action": "买入", "sentiment": "看多"},
        MockPriceProvider({"505050": [DailyBar(old_date + timedelta(days=1), 10.2)]}),
    )
    assert missing_start.get("d1_status") == "数据不足", "missing advice-day close after window arrived must be insufficient"

    missing_target = evaluate_record(
        {"date": old_date.isoformat(), "code": "606060", "action": "买入", "sentiment": "看多"},
        MockPriceProvider({"606060": [DailyBar(old_date, 10.0)]}),
    )
    assert missing_target.get("d1_status") == "数据不足", "missing target close after window arrived must be insufficient"

    existing_verified = evaluate_record(
        {
            "date": old_date.isoformat(),
            "code": "707070",
            "action": "买入",
            "sentiment": "看多",
            "advice_close": 10.0,
            "d1_status": "已验证",
            "d1_close": 10.3,
            "d1_return": 0.03,
            "d1_hit": True,
        },
        MockErrorPriceProvider(),
    )
    assert existing_verified.get("d1_status") == "已验证" and existing_verified.get("d1_close") == 10.3

    short_suspend = evaluate_record(
        {"date": today.isoformat(), "code": "404040", "action": "观望", "sentiment": "震荡"},
        MockPriceProvider({"404040": [DailyBar(today, 10.0)]}),
    )
    assert short_suspend.get("d1_status") == "等待验证", "not enough future effective trading bars must wait"

    class TinyFrame:
        empty = False
        columns = ["日期", "收盘价"]

        def iterrows(self):
            yield 0, {"日期": old_date.isoformat(), "收盘价": "10.5"}

    bars = dataframe_to_bars(TinyFrame())
    assert bars and bars[0].trade_date == old_date and bars[0].close == 10.5

    class EnglishFrame:
        empty = False
        columns = ["datetime", "Close"]

        def iterrows(self):
            yield 0, {"datetime": old_date.isoformat(), "Close": "11.5"}

    english_bars = dataframe_to_bars(EnglishFrame())
    assert english_bars and english_bars[0].trade_date == old_date and english_bars[0].close == 11.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update AI advice backtest outputs without calling any LLM.")
    parser.add_argument("--test-mode", action="store_true", help="Create deterministic mock data and validate the module.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.test_mode:
        provider = setup_test_fixture()
        accuracy = run_backtest(provider=provider)
        assert_test_results(accuracy)
        print("advice backtest test mode passed")
        return 0

    try:
        run_backtest()
    except Exception as exc:
        print(f"ERROR: advice backtest update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
