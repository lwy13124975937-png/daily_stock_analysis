"""Build STOCK_LIST and a public holdings snapshot from stock-dashboard.

The GitHub Actions workflow uses this script before running analysis. It reads
the latest holdings JSON, includes only ``stock`` codes in STOCK_LIST,
and writes a sanitized snapshot for the static Pages builder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import base64
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.reports.contracts import HOLDINGS_SCHEMA_VERSION, write_json_atomic  # noqa: E402
from src.reports.public_holdings import (  # noqa: E402
    ANALYZED_TYPES,
    SNAPSHOT_TYPES,
    build_public_holdings_snapshot,
    normalize_code,
    normalize_holding_type,
    stock_items_from_snapshot,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
SITE_DATA_DIR = ROOT_DIR / "site_data"
SNAPSHOT_PATH = SITE_DATA_DIR / "holdings_snapshot.json"
CURRENT_STOCK_LIST_PATH = SITE_DATA_DIR / "current_stock_list.json"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_HOLDINGS_URL = (
    "https://raw.githubusercontent.com/"
    "lwy13124975937-png/stock-dashboard/main/holdings_data.json"
)
DEFAULT_HOLDINGS_API_URL = (
    "https://api.github.com/repos/"
    "lwy13124975937-png/stock-dashboard/contents/holdings_data.json?ref=main"
)


def _is_enabled(record: dict) -> bool:
    return bool(record.get("enabled", True))


def _normalize_code(value: object) -> str:
    return normalize_code(value)


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_type(value: object) -> str:
    return normalize_holding_type(value)


def _auth_token() -> str:
    for name in (
        "HOLDINGS_DATA_TOKEN",
        "STOCK_DASHBOARD_TOKEN",
        "GH_PAT",
        "GH_TOKEN",
        "PAT_TOKEN",
        "REPO_ACCESS_TOKEN",
        "GITHUB_TOKEN",
    ):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    return ""


def _request_headers() -> dict[str, str]:
    headers = {
        "User-Agent": "daily-stock-analysis-actions",
        "Accept": "application/vnd.github+json, application/json;q=0.9,*/*;q=0.8",
    }
    token = _auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _decode_github_contents_payload(payload: dict) -> dict:
    if not isinstance(payload, dict) or "content" not in payload:
        return payload
    content = str(payload.get("content") or "")
    encoding = str(payload.get("encoding") or "").lower()
    if encoding == "base64":
        decoded = base64.b64decode(content).decode("utf-8")
        return json.loads(decoded)
    return json.loads(content)


def _download_json(url: str) -> dict:
    request = Request(url, headers=_request_headers())
    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = json.loads(response.read().decode(charset))
        return _decode_github_contents_payload(payload)


def _safe_url_for_log(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        return "configured holdings endpoint"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _load_json_from_env() -> tuple[dict, str] | None:
    payload_b64 = os.environ.get("HOLDINGS_DATA_JSON_B64", "").strip()
    if payload_b64:
        try:
            payload = base64.b64decode(payload_b64).decode("utf-8")
            return json.loads(payload), "env:HOLDINGS_DATA_JSON_B64"
        except Exception as exc:
            raise RuntimeError(
                f"failed to parse HOLDINGS_DATA_JSON_B64: {type(exc).__name__}: {exc}"
            ) from exc

    payload = os.environ.get("HOLDINGS_DATA_JSON", "").strip()
    if payload:
        try:
            return json.loads(payload), "env:HOLDINGS_DATA_JSON"
        except Exception as exc:
            raise RuntimeError(
                f"failed to parse HOLDINGS_DATA_JSON: {type(exc).__name__}: {exc}"
            ) from exc

    return None


def _load_holdings_data() -> tuple[dict, str]:
    local_path = os.environ.get("HOLDINGS_DATA_PATH", "").strip()
    if local_path:
        path = Path(local_path)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return json.loads(path.read_text(encoding="utf-8-sig")), str(path)

    env_data = _load_json_from_env()
    if env_data is not None:
        return env_data

    attempts: list[str] = []
    urls = []
    configured_url = os.environ.get("HOLDINGS_DATA_URL", "").strip()
    if configured_url:
        urls.append(configured_url)
    else:
        urls.append(DEFAULT_HOLDINGS_URL)
    configured_api_url = os.environ.get("HOLDINGS_DATA_API_URL", "").strip()
    if configured_api_url:
        urls.append(configured_api_url)
    urls.append(DEFAULT_HOLDINGS_API_URL)

    for url in dict.fromkeys(urls):
        try:
            return _download_json(url), url
        except Exception as exc:
            attempts.append(
                f"- failed holdings data {_safe_url_for_log(url)}: {type(exc).__name__}: {exc}"
            )

    raise RuntimeError(
        "unable to load holdings data; attempted sources:\n" + "\n".join(attempts)
    )


def _empty_account() -> dict[str, list[dict[str, str]]]:
    return {asset_type: [] for asset_type in SNAPSHOT_TYPES}


def _append_unique_code(codes: list[str], seen: set[str], code: str) -> None:
    if code and code not in seen:
        seen.add(code)
        codes.append(code)


def build_holdings_snapshot(data: dict, source_url: str) -> tuple[dict, dict[str, list[str]], list[str]]:
    snapshot, _warnings = build_public_holdings_snapshot(data, source_url)
    type_codes: dict[str, list[str]] = {asset_type: [] for asset_type in SNAPSHOT_TYPES}
    seen_by_type: dict[str, set[str]] = {asset_type: set() for asset_type in SNAPSHOT_TYPES}
    stock_list: list[str] = []
    seen_analysis_codes: set[str] = set()
    for groups in snapshot["accounts"].values():
        for asset_type in SNAPSHOT_TYPES:
            for item in groups[asset_type]:
                code = item["code"]
                _append_unique_code(type_codes[asset_type], seen_by_type[asset_type], code)
                if asset_type in ANALYZED_TYPES:
                    _append_unique_code(stock_list, seen_analysis_codes, code)
    return snapshot, type_codes, stock_list


def write_snapshot(snapshot: dict) -> None:
    write_json_atomic(SNAPSHOT_PATH, snapshot)
    print(f"Holdings snapshot written: {SNAPSHOT_PATH.relative_to(ROOT_DIR)}")


def _stock_items_from_snapshot(snapshot: dict) -> list[dict[str, str]]:
    return stock_items_from_snapshot(snapshot)


def _stock_items_from_codes(stock_list: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_code in stock_list.split(","):
        code = _normalize_code(raw_code)
        if not code or code in seen:
            continue
        seen.add(code)
        items.append({"code": code, "name": code, "type": "stock"})
    return items


def write_current_stock_list(stock_items: list[dict[str, str]], source: str) -> None:
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": HOLDINGS_SCHEMA_VERSION,
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "source_kind": source,
        "source_fingerprint": hashlib.sha256(
            json.dumps(stock_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "stocks": stock_items,
    }
    write_json_atomic(CURRENT_STOCK_LIST_PATH, payload)
    print(f"Current stock list written: {CURRENT_STOCK_LIST_PATH.relative_to(ROOT_DIR)}")


def _write_github_env(name: str, value: str) -> None:
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return
    with Path(github_env).open("a", encoding="utf-8") as env_file:
        env_file.write(f"{name}={value}\n")


def _set_stock_list(value: str, source: str, stock_items: list[dict[str, str]] | None = None) -> str:
    stock_list = value.strip()
    write_current_stock_list(stock_items or _stock_items_from_codes(stock_list), source)
    _write_github_env("STOCK_LIST", stock_list)
    count = len([item for item in stock_list.split(",") if item.strip()])
    fingerprint = hashlib.sha256(stock_list.encode("utf-8")).hexdigest()[:12]
    print(f"STOCK_LIST count={count}, fingerprint={fingerprint}, source={source}")
    return stock_list


def _print_type_codes(label: str, codes: list[str]) -> None:
    print(f"{label}数量: {len(codes)}")


def build_stock_list() -> str:
    try:
        data, source = _load_holdings_data()
        snapshot, type_codes, stock_list = build_holdings_snapshot(data, source)
        write_snapshot(snapshot)
        stock_items = _stock_items_from_snapshot(snapshot)
    except Exception as exc:
        explicit_stock_list = os.environ.get("STOCK_LIST", "").strip()
        if explicit_stock_list:
            if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
                print(
                    "ERROR: full holdings data is required in GitHub Actions; "
                    "refusing to continue with STOCK_LIST only because no holdings snapshot can be generated.",
                    file=sys.stderr,
                )
                print(f"Reason: {type(exc).__name__}: {exc}", file=sys.stderr)
                return ""
            print(
                "WARNING: failed to load full holdings data; using explicit STOCK_LIST only. "
                "No holdings_snapshot.json will be generated.",
                file=sys.stderr,
            )
            print(f"Reason: {type(exc).__name__}: {exc}", file=sys.stderr)
            return _set_stock_list(explicit_stock_list, "env-stock-list")
        print(f"ERROR: failed to load full holdings data: {type(exc).__name__}: {exc}", file=sys.stderr)
        return ""

    _print_type_codes("A股逐只分析", type_codes["stock"])
    _print_type_codes("LOF/ETF 组合复盘", type_codes["lof"])
    _print_type_codes("场外基金清单", type_codes["otc"])
    warning_count = len(snapshot.get("validation_warnings") or [])
    if warning_count:
        print(f"Holdings validation warnings: count={warning_count}", file=sys.stderr)

    if not stock_list:
        print("ERROR: no enabled stock holdings found in holdings data.", file=sys.stderr)
        return ""

    return _set_stock_list(",".join(stock_list), "holdings", stock_items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the current A-share analysis list and sanitized full holdings snapshot",
    )
    parser.parse_args(argv)
    stock_list = build_stock_list()
    return 0 if stock_list else 1


if __name__ == "__main__":
    raise SystemExit(main())
