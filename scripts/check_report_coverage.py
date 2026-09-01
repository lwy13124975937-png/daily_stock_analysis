#!/usr/bin/env python3
"""Validate exact stock-code coverage using immutable current-run artifacts."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.reports.contracts import DataIntegrityError, read_json_strict  # noqa: E402
from src.reports.public_holdings import normalize_code  # noqa: E402
from src.reports.structured_stock_report import validate_structured_stock_report  # noqa: E402


DEFAULT_CURRENT_STOCK_LIST_PATH = ROOT_DIR / "site_data" / "current_stock_list.json"
DEFAULT_REPORTS_DIR = ROOT_DIR / "reports"
REPORT_NAME_RE = re.compile(r"^report_(\d{8})\.json$")


class CoverageValidationError(RuntimeError):
    pass


def latest_structured_report(reports_dir: Path) -> Path | None:
    """Local convenience selection by report date, never filesystem mtime."""

    candidates: list[tuple[str, Path]] = []
    for path in reports_dir.glob("report_*.json"):
        match = REPORT_NAME_RE.fullmatch(path.name)
        if match:
            candidates.append((match.group(1), path))
    return max(candidates, default=("", None), key=lambda item: item[0])[1]


def load_current_stock_codes(path: Path) -> list[str]:
    if not path.exists():
        raise CoverageValidationError(f"current-run stock list missing: {path}")
    try:
        payload = read_json_strict(path)
    except DataIntegrityError as exc:
        raise CoverageValidationError(str(exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("stocks"), list):
        raise CoverageValidationError(f"invalid current stock list schema: {path}")
    codes: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(payload["stocks"]):
        if not isinstance(item, dict):
            raise CoverageValidationError(f"invalid stock entry at index {index}: {path}")
        if str(item.get("type") or "stock").strip().lower() != "stock":
            continue
        code = normalize_code(item.get("code"))
        if not code:
            raise CoverageValidationError(f"stock entry at index {index} has no code: {path}")
        if code in seen:
            raise CoverageValidationError(f"duplicate stock code in current stock list: {code}")
        seen.add(code)
        codes.append(code)
    if not codes:
        raise CoverageValidationError(f"current stock list contains no A-share stock codes: {path}")
    return codes


def load_structured_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CoverageValidationError(f"current-run structured report missing: {path}")
    try:
        payload = read_json_strict(path)
    except DataIntegrityError as exc:
        raise CoverageValidationError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise CoverageValidationError(f"structured report must be an object: {path}")
    try:
        validate_structured_stock_report(payload)
    except ValueError as exc:
        raise CoverageValidationError(f"invalid structured report {path}: {exc}") from exc
    return payload


def validate_coverage(stock_codes: list[str], report: dict[str, Any]) -> None:
    expected = [normalize_code(code) for code in report.get("expected_stock_codes", [])]
    if expected != stock_codes:
        missing = sorted(set(stock_codes) - set(expected))
        unexpected = sorted(set(expected) - set(stock_codes))
        raise CoverageValidationError(
            "analysis input and report expected identities differ; "
            f"missing_in_report={missing}, unexpected_in_report={unexpected}"
        )
    result_codes = [normalize_code(item.get("code")) for item in report.get("results", [])]
    if set(result_codes) != set(stock_codes):
        raise CoverageValidationError("report results do not exactly cover the current-run stock list")
    success_ids = {normalize_code(code) for code in report.get("success_ids", [])}
    failure_ids = {normalize_code(code) for code in report.get("failure_ids", [])}
    if success_ids & failure_ids:
        raise CoverageValidationError("a stock cannot be both success and failure")
    if success_ids | failure_ids != set(stock_codes):
        raise CoverageValidationError("success/failure identities do not partition expected stock codes")


def run_check(*, stock_list_path: Path, report_path: Path) -> None:
    stock_codes = load_current_stock_codes(stock_list_path)
    report = load_structured_report(report_path)
    validate_coverage(stock_codes, report)
    print(
        "Report coverage passed: "
        f"expected={len(stock_codes)}, success={report['success_count']}, failure={report['failure_count']}, "
        f"run_id={report['run_id']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate exact current-run stock report coverage")
    parser.add_argument("--stock-list", type=Path, default=DEFAULT_CURRENT_STOCK_LIST_PATH)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report_path = args.report or latest_structured_report(DEFAULT_REPORTS_DIR)
    if report_path is None:
        print("ERROR: no structured report_YYYYMMDD.json found", file=sys.stderr)
        return 1
    try:
        run_check(stock_list_path=args.stock_list, report_path=report_path)
    except CoverageValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
