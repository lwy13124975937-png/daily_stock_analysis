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
ZERO_SUCCESS_RE = re.compile(r"(成功\s*[:：]\s*0|success\s*[=:]\s*0)", re.IGNORECASE)


def _latest_report(reports_dir: Path) -> Path | None:
    reports = sorted(
        reports_dir.glob("report_*.md"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return reports[0] if reports else None


def _iter_log_files(logs_dir: Path) -> list[Path]:
    if not logs_dir.exists():
        return []
    patterns = ("*.log", "*.txt", "*.out")
    files: list[Path] = []
    for pattern in patterns:
        files.extend(path for path in logs_dir.rglob(pattern) if path.is_file())
    return sorted(set(files), key=lambda path: (path.stat().st_mtime, path.name), reverse=True)


def _contains_zero_success(log_files: list[Path]) -> tuple[bool, Path | None, str]:
    for path in log_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = ZERO_SUCCESS_RE.search(text)
        if match:
            return True, path, match.group(0)
    return False, None, ""


def validate_report(reports_dir: Path, logs_dir: Path, min_bytes: int = MIN_REPORT_BYTES) -> int:
    latest = _latest_report(reports_dir)
    if latest is None:
        print(f"ERROR: no report_*.md found under {reports_dir}", file=sys.stderr)
        return 1

    size = latest.stat().st_size
    if size <= min_bytes:
        print(
            f"ERROR: latest report is too small: {latest} ({size} bytes <= {min_bytes})",
            file=sys.stderr,
        )
        return 1

    has_zero_success, log_path, matched = _contains_zero_success(_iter_log_files(logs_dir))
    if has_zero_success:
        print(
            f"ERROR: analysis log indicates zero successful stocks: {matched} in {log_path}",
            file=sys.stderr,
        )
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
