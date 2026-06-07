"""Validate that a stock analysis report is safe to publish to Pages."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_DIR = ROOT_DIR / "reports"
DEFAULT_LOGS_DIR = ROOT_DIR / "logs"
MIN_REPORT_BYTES = 500
SUCCESS_SUMMARY_RE = re.compile(
    r"(?:成功|success)\s*[:：=]\s*(\d+)\s*[,，]\s*(?:失败|fail(?:ed|ure)?|failed)\s*[:：=]\s*(\d+)",
    re.IGNORECASE,
)


def _latest_report(reports_dir: Path) -> Path | None:
    reports = sorted(
        reports_dir.glob("report_*.md"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return reports[0] if reports else None


def _latest_log(logs_dir: Path) -> Path | None:
    if not logs_dir.exists():
        return None
    debug_logs = [path for path in logs_dir.glob("stock_analysis_debug_*.log") if path.is_file()]
    if debug_logs:
        return max(debug_logs, key=lambda path: (path.stat().st_mtime, path.name))
    analysis_logs = [path for path in logs_dir.glob("stock_analysis_*.log") if path.is_file()]
    if analysis_logs:
        return max(analysis_logs, key=lambda path: (path.stat().st_mtime, path.name))
    return None


def _last_success_summary(log_path: Path) -> tuple[int, int] | None:
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        print(f"WARNING: cannot read latest log {log_path}: {exc}", file=sys.stderr)
        return None
    matches = list(SUCCESS_SUMMARY_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    return int(last.group(1)), int(last.group(2))


def validate_report(reports_dir: Path, logs_dir: Path, min_bytes: int = MIN_REPORT_BYTES) -> int:
    latest = _latest_report(reports_dir)
    if latest is None:
        print(f"ERROR: no report_*.md found under {reports_dir}", file=sys.stderr)
        return 1

    size = latest.stat().st_size
    print(f"Latest report: {latest} ({size} bytes)")
    if size <= min_bytes:
        print(
            f"ERROR: latest report is too small: {latest} ({size} bytes <= {min_bytes})",
            file=sys.stderr,
        )
        return 1

    latest_log = _latest_log(logs_dir)
    if latest_log is None:
        print(f"WARNING: no stock_analysis log found under {logs_dir}; report size check passed")
        print(f"OK: valid stock report found: {latest} ({size} bytes)")
        return 0

    print(f"Latest log: {latest_log}")
    summary = _last_success_summary(latest_log)
    if summary is None:
        print(
            f"WARNING: no success/failure summary found in latest log {latest_log}; "
            "report size check passed"
        )
        print(f"OK: valid stock report found: {latest} ({size} bytes)")
        return 0

    success_count, fail_count = summary
    print(f"Latest analysis summary: success={success_count}, failed={fail_count}")
    if success_count == 0:
        print(f"ERROR: latest analysis log indicates zero successful stocks: {latest_log}", file=sys.stderr)
        return 1

    print(f"OK: valid stock report found: {latest} ({size} bytes)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate generated stock report before Pages deploy.")
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS_DIR)
    parser.add_argument("--min-bytes", type=int, default=MIN_REPORT_BYTES)
    args = parser.parse_args()
    return validate_report(args.reports_dir, args.logs_dir, args.min_bytes)


if __name__ == "__main__":
    raise SystemExit(main())
