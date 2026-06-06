"""Build STOCK_LIST from stock-dashboard holdings for GitHub Actions.

Only holdings with ``type == "stock"`` are included in the first version.
Manual STOCK_LIST values from GitHub Variables/Secrets take precedence.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_HOLDINGS_URL = (
    "https://raw.githubusercontent.com/"
    "lwy13124975937-png/stock-dashboard/main/holdings_data.json"
)
DEFAULT_FALLBACK_STOCK_LIST = "600519"


def _is_enabled(record: dict) -> bool:
    return bool(record.get("enabled", True))


def _normalize_code(value: object) -> str:
    code = str(value or "").strip()
    if code.isdigit() and 0 < len(code) <= 6:
        return code.zfill(6)
    return code


def _download_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "daily-stock-analysis-actions"})
    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def extract_stock_codes(data: dict) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    holdings = data.get("holdings", [])
    if not isinstance(holdings, list):
        return codes

    for record in holdings:
        if not isinstance(record, dict):
            continue
        if not _is_enabled(record):
            continue
        if str(record.get("type", "")).strip().lower() != "stock":
            continue

        code = _normalize_code(record.get("code"))
        if not code or code in seen:
            continue

        seen.add(code)
        codes.append(code)

    return codes


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


def build_stock_list() -> str:
    manual_stock_list = os.environ.get("MANUAL_STOCK_LIST", "").strip()
    if manual_stock_list:
        return _set_stock_list(manual_stock_list, "manual")

    fallback = os.environ.get("STOCK_LIST_FALLBACK", DEFAULT_FALLBACK_STOCK_LIST).strip()
    fallback = fallback or DEFAULT_FALLBACK_STOCK_LIST
    url = os.environ.get("HOLDINGS_DATA_URL", DEFAULT_HOLDINGS_URL).strip()
    url = url or DEFAULT_HOLDINGS_URL

    try:
        data = _download_json(url)
        codes = extract_stock_codes(data)
    except Exception as exc:
        print(f"Failed to download or parse holdings data: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _set_stock_list(fallback, "fallback")

    if not codes:
        print("No enabled stock holdings found; using fallback STOCK_LIST.", file=sys.stderr)
        return _set_stock_list(fallback, "fallback")

    return _set_stock_list(",".join(codes), "holdings")


def main() -> int:
    build_stock_list()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
