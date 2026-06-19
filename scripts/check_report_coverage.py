#!/usr/bin/env python3
"""Check that the latest stock report formally covers stock holdings."""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_stock_list_from_holdings import (  # noqa: E402
    DEFAULT_HOLDINGS_URL,
    build_holdings_snapshot,
    _download_json,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_PATH = ROOT_DIR / "site_data" / "holdings_snapshot.json"
DEFAULT_REPORTS_DIR = ROOT_DIR / "reports"
DEFAULT_HOLDINGS_PATHS = (
    ROOT_DIR / "holdings_data.json",
    ROOT_DIR / "site_data" / "holdings_data.json",
    ROOT_DIR / "data" / "holdings_data.json",
)
REPORTABLE_TYPES = {"stock"}
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.MULTILINE)


def latest_report(reports_dir: Path) -> Path | None:
    reports = list(reports_dir.glob("report_*.md"))
    if not reports:
        return None
    return max(reports, key=lambda path: path.stat().st_mtime)


def _load_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON: {path}: {exc}") from exc


def _write_recovered_snapshot(snapshot_path: Path, snapshot: dict) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"recovered holdings snapshot written: {snapshot_path}")


def load_snapshot(
    snapshot_path: Path,
    holdings_data_path: Path | None = None,
    holdings_url: str | None = None,
) -> dict:
    attempts: list[str] = []

    if snapshot_path.exists():
        try:
            snapshot = _load_json_file(snapshot_path)
            print(f"holdings source: snapshot {snapshot_path}")
            return snapshot
        except RuntimeError as exc:
            attempts.append(str(exc))
    else:
        attempts.append(f"missing snapshot: {snapshot_path}")

    candidate_paths: list[Path] = []
    if holdings_data_path is not None:
        candidate_paths.append(holdings_data_path)
    env_path = os.environ.get("HOLDINGS_DATA_PATH")
    if env_path:
        candidate_paths.append(Path(env_path))
    candidate_paths.extend(DEFAULT_HOLDINGS_PATHS)

    seen_paths: set[Path] = set()
    for candidate in candidate_paths:
        candidate = candidate.expanduser()
        if not candidate.is_absolute():
            candidate = ROOT_DIR / candidate
        candidate = candidate.resolve()
        if candidate in seen_paths:
            continue
        seen_paths.add(candidate)
        if not candidate.exists():
            attempts.append(f"missing holdings data: {candidate}")
            continue
        try:
            data = _load_json_file(candidate)
            snapshot, _type_codes, _stock_list = build_holdings_snapshot(data, str(candidate))
            _write_recovered_snapshot(snapshot_path, snapshot)
            print(f"holdings source: raw file {candidate}")
            return snapshot
        except RuntimeError as exc:
            attempts.append(str(exc))
        except Exception as exc:
            attempts.append(f"failed to load holdings data {candidate}: {type(exc).__name__}: {exc}")

    url = (holdings_url or os.environ.get("HOLDINGS_DATA_URL") or DEFAULT_HOLDINGS_URL).strip()
    if url:
        try:
            data = _download_json(url)
            snapshot, _type_codes, _stock_list = build_holdings_snapshot(data, url)
            _write_recovered_snapshot(snapshot_path, snapshot)
            print(f"holdings source: raw url {url}")
            return snapshot
        except Exception as exc:
            attempts.append(f"failed to download holdings data {url}: {type(exc).__name__}: {exc}")

    detail = "\n".join(f"- {item}" for item in attempts)
    raise RuntimeError(
        "holdings snapshot not found and no fallback holdings data could be loaded. "
        "Run scripts/build_stock_list_from_holdings.py before coverage, or provide "
        "site_data/holdings_snapshot.json / holdings_data.json.\n"
        f"attempted sources:\n{detail}"
    )


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


def section_after_heading(report_text: str, heading_keyword: str) -> str:
    for match in HEADING_RE.finditer(report_text):
        title = match.group(2)
        if heading_keyword not in title:
            continue
        start = match.end()
        next_heading = HEADING_RE.search(report_text, start)
        end = next_heading.start() if next_heading else len(report_text)
        return report_text[start:end]
    return ""


def codes_in_single_analysis_headings(report_text: str) -> set[str]:
    codes = set()
    for match in HEADING_RE.finditer(report_text):
        title = match.group(2)
        if "分析结果摘要" in title or "未完成分析标的" in title:
            continue
        codes.update(CODE_RE.findall(title))
    return codes


def formally_covered_codes(report_text: str) -> Dict[str, set[str]]:
    summary_section = section_after_heading(report_text, "分析结果摘要")
    unfinished_section = section_after_heading(report_text, "未完成分析标的")
    summary_codes = set(CODE_RE.findall(summary_section))
    single_heading_codes = codes_in_single_analysis_headings(report_text)
    unfinished_codes = set(CODE_RE.findall(unfinished_section))
    return {
        "summary": summary_codes,
        "single_analysis_heading": single_heading_codes,
        "unfinished": unfinished_codes,
    }


def has_lof_holdings(snapshot: dict) -> bool:
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(accounts, dict):
        return False
    for groups in accounts.values():
        if not isinstance(groups, dict):
            continue
        holdings = groups.get("lof", [])
        if isinstance(holdings, list) and holdings:
            return True
    return False


def has_lof_portfolio_review(report_text: str) -> bool:
    for match in HEADING_RE.finditer(report_text):
        compact = re.sub(r"\s+", "", match.group(2))
        if "LOF/ETF组合复盘" in compact:
            return True
    return False


def check_coverage(
    snapshot_path: Path,
    reports_dir: Path,
    holdings_data_path: Path | None = None,
    holdings_url: str | None = None,
) -> Tuple[bool, List[Dict[str, str]]]:
    snapshot = load_snapshot(snapshot_path, holdings_data_path, holdings_url)
    required_holdings = unique_by_code(iter_reportable_holdings(snapshot))
    if not required_holdings:
        raise RuntimeError("no reportable stock holdings found in holdings snapshot")

    report_path = latest_report(reports_dir)
    if report_path is None:
        raise RuntimeError(f"no report_*.md files found in {reports_dir}")

    report_text = report_path.read_text(encoding="utf-8", errors="ignore")
    covered_by = formally_covered_codes(report_text)
    success_codes = covered_by["summary"] | covered_by["single_analysis_heading"]
    failed_codes = covered_by["unfinished"]
    covered_codes = success_codes | failed_codes
    missing = [holding for holding in required_holdings if holding["code"] not in covered_codes]

    print(f"latest report: {report_path}")
    print(f"reportable holdings: {len(required_holdings)}")
    print(f"summary-covered codes: {len(covered_by['summary'])}")
    print(f"single-analysis-heading codes: {len(covered_by['single_analysis_heading'])}")
    print(f"unfinished-covered codes: {len(covered_by['unfinished'])}")
    if missing:
        print("ERROR: report is missing formally covered holding codes:")
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
        description="Verify latest report_*.md covers every stock code and includes LOF/ETF portfolio review when needed."
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
    parser.add_argument(
        "--holdings-data",
        type=Path,
        default=None,
        help="Optional raw holdings_data.json path used when the snapshot is missing.",
    )
    parser.add_argument(
        "--holdings-url",
        type=str,
        default=None,
        help="Optional raw holdings_data.json URL used when local sources are missing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        ok, _missing = check_coverage(
            args.snapshot,
            args.reports_dir,
            args.holdings_data,
            args.holdings_url,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
