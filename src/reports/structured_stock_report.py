"""Build and validate the structured counterpart of a stock Markdown report."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.core.session_calendar import ExchangeSessionCalendar, SessionCalendar
from src.reports.contracts import (
    PORTFOLIO_REVIEW_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    ActionCode,
    FailureCode,
    failure_code_from_exception,
    normalize_action,
    normalize_sentiment,
    public_failure_message,
    write_json_atomic,
)
from src.reports.portfolio_review import REQUIRED_SECTIONS


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REPORT_TYPE = "stock_daily"


def normalize_code(value: Any) -> str:
    code = str(value or "").strip()
    return code.zfill(6) if code.isdigit() and 0 < len(code) <= 6 else code


def _clean_public_text(value: Any, *, max_length: int = 1200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_length:
        return text
    boundary = max(text.rfind(mark, 0, max_length + 1) for mark in "。！？；.!?;")
    if boundary >= max_length // 2:
        return text[: boundary + 1] + "（摘要已截取）"
    # A hard character slice would publish a misleading half sentence. Keep the
    # complete source text when no trustworthy sentence boundary is available.
    return text


def _safe_sections(result: Any) -> dict[str, Any]:
    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, Mapping):
        dashboard = {}
    battle_plan = dashboard.get("battle_plan") if isinstance(dashboard.get("battle_plan"), Mapping) else {}
    checklist = battle_plan.get("action_checklist") if isinstance(battle_plan, Mapping) else []
    if not isinstance(checklist, list):
        checklist = []
    return {
        "core_conclusion": _clean_public_text(
            getattr(result, "get_core_conclusion", lambda: getattr(result, "analysis_summary", ""))(),
            max_length=500,
        ),
        "battle_plan": [_clean_public_text(item, max_length=240) for item in checklist[:8] if _clean_public_text(item)],
        "related_sector": _clean_public_text(getattr(result, "sector_position", ""), max_length=500),
        "trend_analysis": _clean_public_text(getattr(result, "trend_analysis", "")),
        "technical_analysis": _clean_public_text(getattr(result, "technical_analysis", "")),
        "fundamental_analysis": _clean_public_text(getattr(result, "fundamental_analysis", "")),
        "risk_warning": _clean_public_text(getattr(result, "risk_warning", ""), max_length=700),
    }


def _success_result(result: Any) -> dict[str, Any]:
    action_raw = _clean_public_text(getattr(result, "operation_advice", ""), max_length=120)
    sentiment_raw = _clean_public_text(getattr(result, "trend_prediction", ""), max_length=120)
    return {
        "code": normalize_code(getattr(result, "code", "")),
        "name": _clean_public_text(getattr(result, "name", ""), max_length=120),
        "success": True,
        "failure_code": FailureCode.NONE.value,
        "action_raw": action_raw,
        "action_normalized": normalize_action(action_raw).value,
        "sentiment_raw": sentiment_raw,
        "sentiment_normalized": normalize_sentiment(sentiment_raw).value,
        "score": getattr(result, "sentiment_score", None),
        "public_summary": _clean_public_text(getattr(result, "analysis_summary", ""), max_length=500),
        "sections": _safe_sections(result),
    }


def _failed_result(item: Mapping[str, Any]) -> dict[str, Any]:
    reason = item.get("reason") or item.get("error_message") or ""
    failure_code = failure_code_from_exception(reason)
    return {
        "code": normalize_code(item.get("code")),
        "name": _clean_public_text(item.get("name") or item.get("code"), max_length=120),
        "success": False,
        "failure_code": failure_code.value,
        "public_message": public_failure_message(failure_code, subject="本标的"),
        "action_raw": "",
        "action_normalized": ActionCode.UNKNOWN.value,
        "sentiment_raw": "",
        "sentiment_normalized": "unknown",
        "score": None,
        "public_summary": "",
        "sections": {},
    }


def build_structured_stock_report(
    *,
    results: Sequence[Any],
    failed_results: Sequence[Mapping[str, Any]],
    expected_stock_codes: Iterable[str],
    generated_at: datetime | None = None,
    run_id: str | None = None,
    report_date: date | None = None,
    calendar: SessionCalendar | None = None,
    markdown_file: str | None = None,
    portfolio_reviews: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    generated = generated_at or datetime.now(SHANGHAI_TZ)
    if generated.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    generated = generated.astimezone(SHANGHAI_TZ)
    session_calendar = calendar or ExchangeSessionCalendar()
    anchor_session = session_calendar.completed_session_at(generated)
    report_day = report_date or generated.date()
    expected = list(dict.fromkeys(normalize_code(code) for code in expected_stock_codes if normalize_code(code)))

    by_code: dict[str, dict[str, Any]] = {}
    for result in results:
        item = _success_result(result)
        if item["code"]:
            by_code[item["code"]] = item
    for failed in failed_results:
        item = _failed_result(failed)
        if item["code"] and item["code"] not in by_code:
            by_code[item["code"]] = item

    missing = [code for code in expected if code not in by_code]
    for code in missing:
        by_code[code] = _failed_result(
            {"code": code, "name": code, "reason": "validation failed: analysis result missing"}
        )

    ordered_results = [by_code[code] for code in expected]
    success_ids = [item["code"] for item in ordered_results if item["success"]]
    failure_ids = [item["code"] for item in ordered_results if not item["success"]]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id or f"stock-{generated.strftime('%Y%m%dT%H%M%S%z')}-{uuid.uuid4().hex[:8]}",
        "generated_at": generated.isoformat(timespec="seconds"),
        "market_data_as_of": anchor_session.isoformat(),
        "anchor_session": anchor_session.isoformat(),
        "report_date": report_day.isoformat(),
        "report_type": REPORT_TYPE,
        "markdown_file": markdown_file,
        "expected_stock_codes": expected,
        "expected_count": len(expected),
        "success_count": len(success_ids),
        "failure_count": len(failure_ids),
        "success_ids": success_ids,
        "failure_ids": failure_ids,
        "status": "complete" if not failure_ids else ("degraded" if success_ids else "failed"),
        "results": ordered_results,
        "portfolio_reviews": [dict(item) for item in portfolio_reviews],
    }
    validate_structured_stock_report(payload)
    return payload


def validate_structured_stock_report(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported structured stock report schema_version")
    if payload.get("report_type") != REPORT_TYPE:
        raise ValueError("invalid structured stock report type")
    expected = [normalize_code(code) for code in payload.get("expected_stock_codes", [])]
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("structured stock report results must be a list")
    result_codes = [normalize_code(item.get("code")) for item in results if isinstance(item, Mapping)]
    if len(result_codes) != len(set(result_codes)):
        raise ValueError("structured stock report contains duplicate result codes")
    if set(result_codes) != set(expected):
        raise ValueError("structured stock report result identities do not match expected_stock_codes")
    success = [item for item in results if isinstance(item, Mapping) and item.get("success") is True]
    failed = [item for item in results if isinstance(item, Mapping) and item.get("success") is False]
    if int(payload.get("expected_count", -1)) != len(expected):
        raise ValueError("structured stock report expected_count mismatch")
    if int(payload.get("success_count", -1)) != len(success):
        raise ValueError("structured stock report success_count mismatch")
    if int(payload.get("failure_count", -1)) != len(failed):
        raise ValueError("structured stock report failure_count mismatch")
    if len(success) + len(failed) != len(expected):
        raise ValueError("structured stock report count arithmetic mismatch")
    if not payload.get("run_id") or not payload.get("generated_at") or not payload.get("anchor_session"):
        raise ValueError("structured stock report metadata is incomplete")
    reviews = payload.get("portfolio_reviews")
    if not isinstance(reviews, list):
        raise ValueError("structured stock report portfolio_reviews must be a list")
    seen_reviews: set[tuple[str, str]] = set()
    for review in reviews:
        if not isinstance(review, Mapping):
            raise ValueError("structured portfolio review must be an object")
        if review.get("schema_version") != PORTFOLIO_REVIEW_SCHEMA_VERSION:
            raise ValueError("structured portfolio review schema_version is invalid")
        if review.get("asset_type") not in {"lof", "otc"}:
            raise ValueError("structured portfolio review has invalid asset_type")
        if review.get("status") not in {"ai", "rule_fallback"}:
            raise ValueError("structured portfolio review has invalid status")
        account = str(review.get("account") or "").strip()
        key = (account, str(review.get("asset_type")))
        if not account or key in seen_reviews:
            raise ValueError("structured portfolio review account identity is missing or duplicated")
        seen_reviews.add(key)
        holdings = review.get("holdings")
        if not isinstance(holdings, list) or not holdings:
            raise ValueError("structured portfolio review holdings are missing")
        sections = review.get("sections")
        required = REQUIRED_SECTIONS[str(review.get("asset_type"))]
        if not isinstance(sections, Mapping) or any(
            not isinstance(sections.get(title), list) or not sections.get(title)
            for title in required
        ):
            raise ValueError("structured portfolio review required sections are incomplete")


def write_structured_stock_report(path: Path, payload: Mapping[str, Any]) -> None:
    validate_structured_stock_report(payload)
    write_json_atomic(path, dict(payload))
