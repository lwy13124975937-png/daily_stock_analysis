"""Validate the structured stock report before any public build."""

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
from src.reports.structured_stock_report import validate_structured_stock_report  # noqa: E402


DEFAULT_REPORTS_DIR = ROOT_DIR / "reports"
REPORT_NAME_RE = re.compile(r"^report_(\d{8})\.json$")


def latest_structured_report(reports_dir: Path) -> Path | None:
    candidates: list[tuple[str, Path]] = []
    for path in reports_dir.glob("report_*.json"):
        match = REPORT_NAME_RE.fullmatch(path.name)
        if match:
            candidates.append((match.group(1), path))
    return max(candidates, default=("", None), key=lambda item: item[0])[1]


def validate_report_file(path: Path) -> dict[str, Any]:
    try:
        payload = read_json_strict(path)
    except DataIntegrityError as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"structured report must be an object: {path}")
    try:
        validate_structured_stock_report(payload)
    except ValueError as exc:
        raise RuntimeError(f"invalid structured report {path}: {exc}") from exc
    if int(payload.get("success_count", 0)) <= 0:
        raise RuntimeError(f"structured report has zero successful stock analyses: {path}")
    expected = int(payload["expected_count"])
    success = int(payload["success_count"])
    failure = int(payload["failure_count"])
    if success + failure != expected:
        raise RuntimeError("structured report count arithmetic is inconsistent")
    if payload.get("status") not in {"complete", "degraded"}:
        raise RuntimeError(f"structured report status is not publishable: {payload.get('status')!r}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a structured stock report before Pages build")
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    path = args.report or latest_structured_report(args.reports_dir)
    if path is None:
        print(f"ERROR: no report_YYYYMMDD.json found under {args.reports_dir}", file=sys.stderr)
        return 1
    try:
        payload = validate_report_file(path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "Structured report valid: "
        f"{path} run_id={payload['run_id']} expected={payload['expected_count']} "
        f"success={payload['success_count']} failure={payload['failure_count']} status={payload['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
