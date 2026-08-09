"""Build STOCK_LIST and a public holdings snapshot from stock-dashboard.

The GitHub Actions workflow uses this script before running analysis. It reads
the latest holdings JSON, includes only ``stock`` codes in STOCK_LIST,
and writes a sanitized snapshot for the static Pages builder.
"""

from __future__ import annotations

import json
import os
import sys
import base64
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


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
ANALYZED_TYPES = {"stock"}
SNAPSHOT_TYPES = ("stock", "lof", "otc")
TYPE_ALIASES = {
    "stock": "stock",
    "a股": "stock",
    "a股个股": "stock",
    "lof": "lof",
    "etf": "lof",
    "lof/etf": "lof",
    "场内基金": "lof",
    "场内基金/etf/lof": "lof",
    "场内基金/ETF/LOF": "lof",
    "otc": "otc",
    "fund": "otc",
    "场外基金": "otc",
}
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


def _normalize_type(value: object) -> str:
    raw = _clean_text(value)
    lowered = raw.lower()
    return TYPE_ALIASES.get(lowered) or TYPE_ALIASES.get(raw) or lowered


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
            attempts.append(f"- failed holdings data {url}: {type(exc).__name__}: {exc}")

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

        asset_type = _normalize_type(record.get("type"))
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
        "generated_at": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
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


def _stock_items_from_snapshot(snapshot: dict) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(accounts, dict):
        return items

    for account_name, groups in accounts.items():
        if not isinstance(groups, dict):
            continue
        stocks = groups.get("stock", [])
        if not isinstance(stocks, list):
            continue
        for holding in stocks:
            if not isinstance(holding, dict):
                continue
            code = _normalize_code(holding.get("code"))
            if not code or code in seen:
                continue
            seen.add(code)
            items.append(
                {
                    "code": code,
                    "name": _clean_text(holding.get("name")) or code,
                    "type": "stock",
                    "account": _clean_text(holding.get("account") or account_name),
                }
            )
    return items


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
        "generated_at": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "stocks": stock_items,
    }
    CURRENT_STOCK_LIST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
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
    print(f"STOCK_LIST={stock_list}")
    print(f"STOCK_LIST source: {source}")
    return stock_list


def _print_type_codes(label: str, codes: list[str]) -> None:
    joined = ",".join(codes) if codes else "(none)"
    print(f"{label}数量: {len(codes)}")
    print(f"{label}代码: {joined}")


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

    if not stock_list:
        print("ERROR: no enabled stock holdings found in holdings data.", file=sys.stderr)
        return ""

    return _set_stock_list(",".join(stock_list), "holdings", stock_items)


def main() -> int:
    stock_list = build_stock_list()
    return 0 if stock_list else 1


if __name__ == "__main__":
    raise SystemExit(main())
