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

    def _manager_instance(self):
        if self._manager is None:
            from data_provider import DataFetcherManager

            self._manager = DataFetcherManager()
        return self._manager

    def get_bars(self, code: str, analysis_date: date) -> tuple[list[DailyBar], str | None]:
        start = (analysis_date - timedelta(days=10)).strftime("%Y-%m-%d")
        end = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
        try:
            df, _source = self._manager_instance().get_daily_data(
                code,
                start_date=start,
                end_date=end,
                days=self.days,
            )
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"
        return dataframe_to_bars(df), None


class MockPriceProvider(PriceProvider):
    def __init__(self, bars_by_code: dict[str, list[DailyBar]]):
        self.bars_by_code = bars_by_code

    def get_bars(self, code: str, analysis_date: date) -> tuple[list[DailyBar], str | None]:
        return list(self.bars_by_code.get(code, [])), None


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


def latest_stock_report(reports_dir: Path = REPORTS_DIR) -> Path | None:
    reports = [p for p in reports_dir.glob("report_20*.md") if p.is_file()]
    if not reports:
        return None
    return max(reports, key=lambda path: (path.stat().st_mtime, path.name))


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
    r"\s*(?:A股个股)?\s*[：:]\s*(?P<rest>.+?)\s*$"
)


def parse_advice_line(line: str) -> tuple[str, str, str, int | None, str, str] | None:
    text = clean_text(line).replace("｜", "|")
    match = ADVICE_RE.match(text)
    if not match:
        return None

    code = normalize_code(match.group("code"))
    name = clean_text(match.group("name")).lstrip("-+* ")
    rest = clean_text(match.group("rest")).replace("｜", "|")
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
    columns = {str(col).lower(): col for col in getattr(df, "columns", [])}
    date_col = columns.get("date") or columns.get("trade_date")
    close_col = columns.get("close") or columns.get("收盘")
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


def evaluate_record(record: dict[str, Any], provider: PriceProvider) -> dict[str, Any]:
    result = dict(record)
    analysis_date = parse_date(result.get("date"))
    group = classify_advice(result)
    result["action_group"] = group
    if analysis_date is None:
        for period in PERIODS:
            result[f"{period}_status"] = "数据不足"
            result[f"{period}_hit"] = None
        return result

    bars, error = provider.get_bars(str(result.get("code")), analysis_date)
    bars = sorted((bar for bar in bars if bar.close > 0), key=lambda bar: bar.trade_date)
    if not bars:
        for period in PERIODS:
            result[f"{period}_status"] = "数据不足"
            result[f"{period}_hit"] = None
            result[f"{period}_close"] = None
            result[f"{period}_return"] = None
        return result

    start_candidates = [bar for bar in bars if bar.trade_date <= analysis_date]
    if not start_candidates:
        for period in PERIODS:
            result[f"{period}_status"] = "数据不足" if error else "等待验证"
            result[f"{period}_hit"] = None
        return result

    advice_close = start_candidates[-1].close
    result["advice_close"] = round(advice_close, 4)
    forward = [bar for bar in bars if bar.trade_date > analysis_date]
    for period, offset in PERIODS.items():
        if len(forward) < offset:
            result[f"{period}_status"] = "数据不足" if error and not forward else "等待验证"
            result[f"{period}_hit"] = None
            result[f"{period}_close"] = None
            result[f"{period}_return"] = None
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
    for record in history:
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
    return f"""
<article class="record-card">
  <h4>{escape(str(record.get('name') or ''))}（{escape(str(record.get('code') or ''))}）</h4>
  <p class="muted">{escape(str(record.get('date') or ''))} · {escape(str(record.get('account') or ''))} · {current}</p>
  <p>建议：{escape(str(record.get('action') or 'unknown'))}｜评分 {escape(score_text)}｜{escape(str(record.get('sentiment') or 'unknown'))}</p>
  <div class="period-grid">
    <span>T+1：{escape(status_text(record, 'd1'))} {escape(format_return(record.get('d1_return')))}</span>
    <span>T+5：{escape(status_text(record, 'd5'))} {escape(format_return(record.get('d5_return')))}</span>
    <span>T+20：{escape(status_text(record, 'd20'))} {escape(format_return(record.get('d20_return')))}</span>
  </div>
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


def render_html(accuracy: dict[str, Any]) -> str:
    records = accuracy.get("records", [])
    current_records = [record for record in records if record.get("is_current_holding_now")]
    recent_records = accuracy.get("recent_records", [])
    miss_cases = accuracy.get("miss_cases", [])

    current_html = "".join(render_record_card(record) for record in current_records[:30]) or '<p class="muted">暂无当前持仓建议样本。</p>'
    all_html = "".join(render_record_card(record) for record in records[-80:][::-1]) or '<p class="muted">暂无历史建议样本。</p>'
    recent_html = "".join(render_record_card(record) for record in recent_records) or '<p class="muted">暂无最近建议。</p>'
    miss_html = "".join(render_record_card(record) for record in miss_cases) or '<p class="muted">暂无已验证未命中样本。</p>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI 建议准确性回测</title>
  <style>
    :root {{ color-scheme: light; --border:#d8dee8; --muted:#5f6b7a; --bg:#f6f8fb; --card:#fff; --text:#111827; --accent:#0b65d8; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.7; color:var(--text); background:var(--bg); }}
    main {{ max-width:960px; margin:0 auto; padding:24px 16px 48px; }}
    a {{ color:var(--accent); }}
    .hero,.panel,.metric-card,.record-card,.small-card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:18px; margin:0 0 18px; }}
    h1 {{ font-size:28px; margin:0 0 8px; }}
    h2 {{ font-size:21px; margin:0 0 12px; }}
    h3,h4 {{ margin:0 0 8px; }}
    .muted {{ color:var(--muted); }}
    .metric-grid,.period-grid,.card-grid {{ display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }}
    .metric-grid div,.period-grid span {{ background:#f8fafc; border:1px solid #e5eaf2; border-radius:8px; padding:10px; overflow-wrap:anywhere; }}
    .metric-grid span,.metric-grid em {{ display:block; color:var(--muted); font-style:normal; }}
    .metric-grid strong {{ display:block; font-size:24px; }}
    .record-card h4 {{ font-size:17px; }}
    .record-card p {{ margin:6px 0; overflow-wrap:anywhere; }}
    .small-card p {{ margin:4px 0; }}
    footer {{ color:var(--muted); border-top:1px solid var(--border); margin-top:24px; padding-top:16px; }}
    @media (max-width: 640px) {{ main {{ padding:16px; }} h1 {{ font-size:24px; }} .hero,.panel,.metric-card,.record-card,.small-card {{ padding:14px; }} }}
  </style>
</head>
<body>
<main>
  <nav><a href="index.html">返回首页</a></nav>
  <header class="hero">
    <h1>AI 建议准确性回测</h1>
    <p class="muted">更新时间：{escape(str(accuracy.get('updated_at') or now_iso()))}</p>
    <p class="muted">不调用 Gemini 或任何 LLM，只用历史建议、当前持仓快照和后续真实行情做规则回测。</p>
  </header>

  <section class="card-grid">
    {render_summary_card("全部历史建议", accuracy.get("summary_all_history", {}))}
    {render_summary_card("当前持仓建议", accuracy.get("summary_current_holdings", {}))}
  </section>

  <section class="panel">
    <h2>分周期统计</h2>
    <div class="card-grid">{render_period_stats(accuracy.get("summary_all_history", {}))}</div>
  </section>

  <section class="panel">
    <h2>按建议类型统计</h2>
    <div class="card-grid">{render_action_stats(accuracy.get("by_action_all_history", {}))}</div>
  </section>

  <section class="panel">
    <h2>当前持仓建议回看</h2>
    {current_html}
  </section>

  <section class="panel">
    <h2>历史全部建议回测</h2>
    {all_html}
  </section>

  <section class="panel">
    <h2>最近建议回看</h2>
    {recent_html}
  </section>

  <section class="panel">
    <h2>最近错误案例</h2>
    {miss_html}
  </section>

  <section class="panel">
    <h2>数据不足说明</h2>
    <p>如果后续第 N 个有效交易日尚未出现，显示“等待验证”；如果行情数据源没有返回建议日或后续收盘价，显示“数据不足”。样本不足时不显示 0% 命中率。</p>
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
    lines = [
        f"# {report_date} AI 建议准确性回测",
        "",
        "本报告不调用 Gemini 或任何 LLM，只回看历史建议与后续真实行情的一致性。",
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
    current = [record for record in accuracy.get("records", []) if record.get("is_current_holding_now")]
    if not current:
        lines.append("暂无当前持仓建议样本。")
    for record in current[:30]:
        lines.append(f"- {record.get('date')} {record.get('name')}({record.get('code')})：{record.get('action')}，T+1 {status_text(record, 'd1')}，T+5 {status_text(record, 'd5')}，T+20 {status_text(record, 'd20')}")
    lines.append("")

    lines.extend(["## 历史全部建议回测", ""])
    for record in accuracy.get("recent_records", []):
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
    snapshot = load_snapshot()
    holdings = current_stock_holdings(snapshot)
    report_path = latest_stock_report()
    new_records = extract_advice_from_report(report_path, holdings) if report_path else []
    history = merge_history([*load_history(), *new_records])
    current_codes = set(holdings)
    evaluated = evaluate_records(history, provider or DataFetcherPriceProvider(), current_codes)
    accuracy = build_accuracy(evaluated)
    report_date = report_date_text(report_path) if report_path else datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    write_outputs(evaluated, accuracy, report_date)
    return accuracy


def assert_test_results(accuracy: dict[str, Any]) -> None:
    records = accuracy.get("records", [])
    codes = {record.get("code") for record in records}
    assert "333333" not in codes, "LOF/ETF must not enter advice backtest"
    assert "121212" not in codes, "OTC must not enter advice backtest"
    assert "999999" in codes, "new stock holding should enter advice history"
    assert "444444" in codes, "sold stock historical advice should remain"
    assert len([r for r in records if r.get("code") == "666666"]) == 2, "different dates for same code must be kept"
    assert any(r.get("code") == "777777" and r.get("d5_status") == "等待验证" for r in records)
    assert any(r.get("code") == "888888" and r.get("d1_status") == "数据不足" for r in records)
    assert accuracy["summary_all_history"]["total_advice"] >= 9
    assert "买入类" in accuracy["by_action_all_history"]
    assert accuracy["recent_records"]
    assert accuracy["miss_cases"]
    assert (SITE_DIR / "advice_backtest.html").exists()
    assert (REPORTS_DIR / "advice_accuracy_20990110.md").exists()
    assert (SITE_DIR / "index.html").read_text(encoding="utf-8").find("advice_backtest.html") != -1


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
