"""Shared versioned contracts for public reports.

The module intentionally has no dependency on the LLM or market-data stack so
validators, migration tools, and renderers can import it safely.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


REPORT_SCHEMA_VERSION = 2
ADVICE_HISTORY_SCHEMA_VERSION = 2
ADVICE_EVALUATION_VERSION = "2.0.0"
HOLDINGS_SCHEMA_VERSION = 2
PORTFOLIO_REVIEW_SCHEMA_VERSION = 1
BUILD_MANIFEST_SCHEMA_VERSION = 2
SITE_RENDERER_VERSION = "2.0.0"


class DataIntegrityError(RuntimeError):
    """Raised when a persisted/public data contract cannot be trusted."""


class FailureCode(str, Enum):
    NONE = "none"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    LLM_FAILED = "llm_failed"
    LLM_TRUNCATED = "llm_truncated"
    INVALID_RESPONSE = "invalid_response"
    SOURCE_SCHEMA_CHANGED = "source_schema_changed"
    MARKET_DATA_MISSING = "market_data_missing"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSUPPORTED_SECTOR_MODEL = "unsupported_sector_model"
    VALIDATION_FAILED = "validation_failed"
    UNKNOWN_INTERNAL = "unknown_internal"


class ActionCode(str, Enum):
    BUY = "buy"
    INCREASE = "increase"
    SELL = "sell"
    REDUCE = "reduce"
    HOLD = "hold"
    HOLD_WATCH = "hold_watch"
    OBSERVE = "observe"
    UNKNOWN = "unknown"


class SentimentCode(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


_SPACE_PUNCT_RE = re.compile(r"[\s|｜,，。；;：:、/\\()（）\[\]【】]+")


def compact_label(value: Any) -> str:
    return _SPACE_PUNCT_RE.sub("", str(value or "").strip()).lower()


def normalize_action(value: Any) -> ActionCode:
    """Normalize the action field only; sentiment must never alter it."""

    text = compact_label(value)
    if not text or text in {"unknown", "未知", "无"}:
        return ActionCode.UNKNOWN

    # Negated actions are resolved before positive keyword matching.
    if any(token in text for token in ("不建议买入", "暂不买入", "不宜买入", "不宜追涨", "避免追涨")):
        return ActionCode.OBSERVE
    if any(token in text for token in ("暂不卖出", "不建议卖出", "无需卖出", "不必卖出")):
        return ActionCode.HOLD
    if any(token in text for token in ("暂不减仓", "不建议减仓", "无需减仓", "不必减仓")):
        return ActionCode.HOLD
    if any(token in text for token in ("暂不加仓", "不建议加仓", "不宜加仓", "不再加仓")):
        return ActionCode.HOLD_WATCH

    if "持有观察" in text or "持仓观察" in text:
        return ActionCode.HOLD_WATCH
    if any(token in text for token in ("等待", "观望", "暂缓", "谨慎观察")):
        return ActionCode.OBSERVE
    if any(token in text for token in ("继续持有", "维持持有", "持有")):
        return ActionCode.HOLD
    if any(token in text for token in ("减仓", "降低仓位", "降低持仓")):
        return ActionCode.REDUCE
    if any(token in text for token in ("卖出", "清仓", "退出持仓")):
        return ActionCode.SELL
    if any(token in text for token in ("加仓", "增持")):
        return ActionCode.INCREASE
    if any(token in text for token in ("买入", "建仓")):
        return ActionCode.BUY
    return ActionCode.UNKNOWN


def normalize_sentiment(value: Any) -> SentimentCode:
    """Normalize sentiment independently from the recommended action."""

    text = compact_label(value)
    if not text or text in {"unknown", "未知", "无"}:
        return SentimentCode.UNKNOWN
    if any(token in text for token in ("不看多", "并非看多", "非看多")):
        return SentimentCode.NEUTRAL
    if any(token in text for token in ("不看空", "并非看空", "非看空")):
        return SentimentCode.NEUTRAL
    if any(token in text for token in ("强烈看多", "看多", "偏多", "乐观", "积极")):
        return SentimentCode.BULLISH
    if any(token in text for token in ("强烈看空", "看空", "偏空", "悲观", "消极")):
        return SentimentCode.BEARISH
    if any(token in text for token in ("中性", "震荡", "分化", "平稳")):
        return SentimentCode.NEUTRAL
    return SentimentCode.UNKNOWN


def failure_code_from_exception(value: Any) -> FailureCode:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if not text:
        return FailureCode.UNKNOWN_INTERNAL
    if any(token in text for token in ("429", "resource_exhausted", "quota", "too many requests")):
        return FailureCode.RATE_LIMITED
    if any(token in text for token in ("503", "serviceunavailable", "high demand", "overloaded")):
        return FailureCode.PROVIDER_UNAVAILABLE
    if any(token in text for token in ("timeout", "timed out")):
        return FailureCode.TIMEOUT
    if any(token in text for token in ("truncated", "输出疑似截断", "模型输出疑似截断")):
        return FailureCode.LLM_TRUNCATED
    if any(token in text for token in ("all llm models failed", "geminiexception", "llm")):
        return FailureCode.LLM_FAILED
    return FailureCode.UNKNOWN_INTERNAL


def public_failure_message(code: FailureCode, *, subject: str = "本次处理") -> str:
    messages = {
        FailureCode.RATE_LIMITED: "模型服务额度受限，未能完成。",
        FailureCode.PROVIDER_UNAVAILABLE: "模型服务暂不可用，未能完成。",
        FailureCode.TIMEOUT: "请求超时，未能完成。",
        FailureCode.LLM_TRUNCATED: "模型输出疑似截断，未采用不完整内容。",
        FailureCode.MARKET_DATA_MISSING: "行情数据不足，当前无法验证。",
        FailureCode.INSUFFICIENT_EVIDENCE: "证据不足，当前无法形成可靠结论。",
        FailureCode.UNSUPPORTED_SECTOR_MODEL: "行业模型证据不足，当前不纳入候选。",
        FailureCode.SOURCE_SCHEMA_CHANGED: "数据源结构异常，当前结果已停止发布。",
        FailureCode.VALIDATION_FAILED: "产物校验失败，当前结果已停止发布。",
        FailureCode.INVALID_RESPONSE: "返回内容无效，未采用该结果。",
        FailureCode.LLM_FAILED: "模型调用失败，未能完成。",
        FailureCode.UNKNOWN_INTERNAL: "处理失败，未能完成。",
    }
    return f"{subject}{messages.get(code, messages[FailureCode.UNKNOWN_INTERNAL])}"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json_strict(path: Path) -> Any:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        return json.loads(text)
    except UnicodeDecodeError as exc:
        raise DataIntegrityError(f"invalid UTF-8 JSON: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DataIntegrityError(f"invalid JSON: {path}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise DataIntegrityError(f"cannot read JSON: {path}: {exc}") from exc


def read_jsonl_strict_bytes(payload: bytes, *, source: str) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataIntegrityError(f"invalid UTF-8 JSONL: {source}: {exc}") from exc

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DataIntegrityError(
                f"invalid JSONL: {source}:{line_number}:{exc.colno}: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise DataIntegrityError(f"invalid JSONL object: {source}:{line_number}: expected object")
        records.append(value)
    return records


def read_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    try:
        return read_jsonl_strict_bytes(path.read_bytes(), source=str(path))
    except OSError as exc:
        raise DataIntegrityError(f"cannot read JSONL: {path}: {exc}") from exc


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_jsonl_atomic(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n")
    temp.replace(path)


ADVICE_PUBLIC_FIELDS = (
    "schema_version",
    "evaluation_version",
    "recommendation_id",
    "run_id",
    "revision",
    "official",
    "date",
    "report_date",
    "anchor_session",
    "anchor_precision",
    "anchor_assumption",
    "generated_at",
    "market_data_as_of",
    "code",
    "name",
    "type",
    "accounts",
    "action_raw",
    "action_normalized",
    "sentiment_raw",
    "sentiment_normalized",
    "score",
    "summary_raw",
    "summary",
    "source_report",
    "holding_snapshot_date",
    "is_current_holding_when_advised",
    "is_current_holding_now",
    "advice_close",
    "advice_close_date",
    "created_at",
    "price_warning",
    "data_status",
    "failure_code",
    "d1_status",
    "d1_close",
    "d1_date",
    "d1_return",
    "d1_direction_hit",
    "d1_direction_miss_reason",
    "d1_sentiment_aligned",
    "d1_observe_consistent",
    "d1_observe_reason",
    "d1_hold_drawdown_flag",
    "d5_status",
    "d5_close",
    "d5_date",
    "d5_return",
    "d5_direction_hit",
    "d5_direction_miss_reason",
    "d5_sentiment_aligned",
    "d5_observe_consistent",
    "d5_observe_reason",
    "d5_hold_drawdown_flag",
    "d20_status",
    "d20_close",
    "d20_date",
    "d20_return",
    "d20_direction_hit",
    "d20_direction_miss_reason",
    "d20_sentiment_aligned",
    "d20_observe_consistent",
    "d20_observe_reason",
    "d20_hold_drawdown_flag",
)


def public_advice_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize an advice record by whitelist, including nested list cleanup."""

    result = {field: record.get(field) for field in ADVICE_PUBLIC_FIELDS if field in record}
    accounts = result.get("accounts")
    if accounts is not None:
        if not isinstance(accounts, (list, tuple)):
            accounts = [accounts]
        result["accounts"] = sorted({str(item).strip() for item in accounts if str(item).strip()})
    result["schema_version"] = ADVICE_HISTORY_SCHEMA_VERSION
    result["evaluation_version"] = ADVICE_EVALUATION_VERSION
    return result
