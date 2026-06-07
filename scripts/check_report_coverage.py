#!/usr/bin/env python3
"""Check that the latest stock report mentions every reportable holding code."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_PATH = ROOT_DIR / "site_data" / "holdings_snapshot.json"
DEFAULT_REPORTS_DIR = ROOT_DIR / "reports"
REPORTABLE_TYPES = {"stock", "lof"}
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def latest_report(reports_dir: Path) -> Path | None:
    reports = list(reports_dir.glob("report_*.md"))
    if not reports:
        return None
    return max(reports, key=lambda path: path.stat().st_mtime)


def load_snapshot(snapshot_path: Path) -> dict:
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"holdings snapshot not found: {snapshot_path}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid holdings snapshot JSON: {snapshot_path}: {exc}") from exc


def iter_reportable_holdings(snapshot: dict) -> Iterable[Dict[str, str]]:
    accounts = snapshot.get("accounts", {})
    if not isinstance(accounts, dict):
        return

    for account_name, grouped_holdings in accounts.items():
        if not isinstance(grouped_holdings, dict):
            continue
        for holding_type, holdings in grouped_holdings.items():
            normalized_type = str(holding_type or "").strip().lower()
            if normalized_type not in REPORTABLE_TYPES:
                continue
            if not isinstance(holdings, list):
                continue
            for holding in holdings:
                if not isinstance(holding, dict):
                    continue
                code = str(holding.get("code", "") or "").strip()
                if not code:
                    continue
                yield {
                    "account": str(account_name or "").strip(),
                    "type": normalized_type,
                    "name": str(holding.get("name", "") or code).strip() or code,
                    "code": code,
                }


def unique_by_code(holdings: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    unique: List[Dict[str, str]] = []
    for holding in holdings:
        code = holding["code"]
        if code in seen:
            continue
        seen.add(code)
        unique.append(holding)
    return unique


def check_coverage(snapshot_path: Path, reports_dir: Path) -> Tuple[bool, List[Dict[str, str]]]:
    snapshot = load_snapshot(snapshot_path)
    required_holdings = unique_by_code(iter_reportable_holdings(snapshot))
    if not required_holdings:
        raise RuntimeError("no reportable stock/lof holdings found in holdings snapshot")

    report_path = latest_report(reports_dir)
    if report_path is None:
        raise RuntimeError(f"no report_*.md files found in {reports_dir}")

    report_text = report_path.read_text(encoding="utf-8", errors="ignore")
    report_codes = set(CODE_RE.findall(report_text))
    missing = [holding for holding in required_holdings if holding["code"] not in report_codes]

    print(f"latest report: {report_path}")
    print(f"reportable holdings: {len(required_holdings)}")
    print(f"codes found in report: {len(report_codes)}")
    if missing:
        print("ERROR: report is missing holding codes:")
        for holding in missing:
            print(
                "- "
                f"{holding['name']}({holding['code']}) "
                f"account={holding['account']} type={holding['type']}"
            )
        return False, missing

    print("report coverage check passed")
    return True, []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify latest report_*.md covers every stock/lof code from holdings_snapshot.json."
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT_PATH,
        help=f"Path to holdings snapshot JSON. Default: {DEFAULT_SNAPSHOT_PATH}",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help=f"Directory containing report_*.md files. Default: {DEFAULT_REPORTS_DIR}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        ok, _missing = check_coverage(args.snapshot, args.reports_dir)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
