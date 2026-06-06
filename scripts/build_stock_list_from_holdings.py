"""Build STOCK_LIST and a public holdings snapshot from stock-dashboard.

The GitHub Actions workflow uses this script before running analysis. It reads
the latest holdings JSON, includes ``stock`` and ``lof`` codes in STOCK_LIST,
and writes a sanitized snapshot for the static Pages builder.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
SITE_DATA_DIR = ROOT_DIR / "site_data"
SNAPSHOT_PATH = SITE_DATA_DIR / "holdings_snapshot.json"

DEFAULT_HOLDINGS_URL = (
    "https://raw.githubusercontent.com/"
    "lwy13124975937-png/stock-dashboard/main/holdings_data.json"
)
DEFAULT_FALLBACK_STOCK_LIST = "600519"
ANALYZED_TYPES = {"stock", "lof"}
SNAPSHOT_TYPES = ("stock", "lof", "otc")
TYPE_LABELS = {
    "stock": "A股个股",
    "lof": "场内基金/ETF/LOF",
    "otc": "场外基金",
}


def _is_enabled(record: dict) -> bool:
    return bool(record.get("enabled", True))


def _normalize_code(value: object) -> str:
    code = str(value or "").strip()
    if code.isdigit() and 0 < len(code) <= 6:
        return code.zfill(6)
    return code


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _download_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "daily-stock-analysis-actions"})
    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def _empty_account() -> dict[str, list[dict[str, str]]]:
    return {asset_type: [] for asset_type in SNAPSHOT_TYPES}


def _append_unique_code(codes: list[str], seen: set[str], code: str) -> None:
    if code and code not in seen:
        seen.add(code)
        codes.append(code)


def build_holdings_snapshot(data: dict, source_url: str) -> tuple[dict, dict[str, list[str]], list[str]]:
    accounts: dict[str, dict[str, list[dict[str, str]]]] = {}
    type_codes: dict[str, list[str]] = {asset_type: [] for asset_type in SNAPSHOT_TYPES}
    seen_by_type: dict[str, set[str]] = {asset_type: set() for asset_type in SNAPSHOT_TYPES}
    stock_list: list[str] = []
    seen_analysis_codes: set[str] = set()

    holdings = data.get("holdings", [])
    if not isinstance(holdings, list):
        holdings = []

    for record in holdings:
        if not isinstance(record, dict) or not _is_enabled(record):
            continue

        asset_type = _clean_text(record.get("type")).lower()
        if asset_type not in SNAPSHOT_TYPES:
            continue

        code = _normalize_code(record.get("code"))
        name = _clean_text(record.get("name"))
        account = _clean_text(record.get("account")) or "未分组账户"
        if not code:
            continue

        public_item = {
            "account": account,
            "type": asset_type,
            "name": name,
            "code": code,
        }
        accounts.setdefault(account, _empty_account())[asset_type].append(public_item)
        _append_unique_code(type_codes[asset_type], seen_by_type[asset_type], code)

        if asset_type in ANALYZED_TYPES:
            _append_unique_code(stock_list, seen_analysis_codes, code)

    snapshot = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_url": source_url,
        "accounts": accounts,
        "type_labels": TYPE_LABELS,
    }
    return snapshot, type_codes, stock_list


def write_snapshot(snapshot: dict) -> None:
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Holdings snapshot written: {SNAPSHOT_PATH.relative_to(ROOT_DIR)}")


def _write_github_env(name: str, value: str) -> None:
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return
    with Path(github_env).open("a", encoding="utf-8") as env_file:
        env_file.write(f"{name}={value}\n")


def _set_stock_list(value: str, source: str) -> str:
    stock_list = value.strip()
    _write_github_env("STOCK_LIST", stock_list)
    print(f"STOCK_LIST={stock_list}")
    print(f"STOCK_LIST source: {source}")
    return stock_list


def _print_type_codes(label: str, codes: list[str]) -> None:
    joined = ",".join(codes) if codes else "(none)"
    print(f"{label} count: {len(codes)}")
    print(f"{label} codes: {joined}")


def build_stock_list() -> str:
    fallback = os.environ.get("STOCK_LIST_FALLBACK", DEFAULT_FALLBACK_STOCK_LIST).strip()
    fallback = fallback or DEFAULT_FALLBACK_STOCK_LIST
    url = os.environ.get("HOLDINGS_DATA_URL", DEFAULT_HOLDINGS_URL).strip()
    url = url or DEFAULT_HOLDINGS_URL

    try:
        data = _download_json(url)
        snapshot, type_codes, stock_list = build_holdings_snapshot(data, url)
        write_snapshot(snapshot)
    except Exception as exc:
        print(f"Failed to download or parse holdings data: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _set_stock_list(fallback, "fallback")

    _print_type_codes("stock", type_codes["stock"])
    _print_type_codes("lof", type_codes["lof"])
    _print_type_codes("otc", type_codes["otc"])

    if not stock_list:
        print("No enabled stock or lof holdings found; using fallback STOCK_LIST.", file=sys.stderr)
        return _set_stock_list(fallback, "fallback")

    return _set_stock_list(",".join(stock_list), "holdings")


def main() -> int:
    build_stock_list()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
