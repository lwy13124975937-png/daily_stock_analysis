"""Canonical public holdings schema and source-safety helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from src.reports.contracts import HOLDINGS_SCHEMA_VERSION


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SNAPSHOT_TYPES = ("stock", "lof", "otc")
ANALYZED_TYPES = {"stock"}
TYPE_LABELS = {
    "stock": "A股个股",
    "lof": "场内基金/ETF/LOF",
    "otc": "场外基金",
}
TYPE_ALIASES = {
    "stock": "stock",
    "a股": "stock",
    "a股个股": "stock",
    "lof": "lof",
    "etf": "lof",
    "lof/etf": "lof",
    "场内基金": "lof",
    "场内基金/etf/lof": "lof",
    "otc": "otc",
    "fund": "otc",
    "场外基金": "otc",
}
ALLOWED_SOURCE_HOSTS = {"raw.githubusercontent.com", "api.github.com", "github.com"}
ALLOWED_SOURCE_REPOSITORY = "lwy13124975937-png/stock-dashboard"
PUBLIC_HOLDING_FIELDS = ("account", "type", "name", "code")


class HoldingsSchemaError(ValueError):
    pass


def normalize_code(value: Any) -> str:
    code = str(value or "").strip()
    return code.zfill(6) if code.isdigit() and 0 < len(code) <= 6 else code


def normalize_holding_type(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    return TYPE_ALIASES.get(lowered) or TYPE_ALIASES.get(raw) or lowered


def public_source_descriptor(source: str, payload: Any) -> dict[str, Any]:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    raw = str(source or "").strip()
    parsed = urlparse(raw)
    allowed_url = None
    if parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() in ALLOWED_SOURCE_HOSTS:
        normalized_path = parsed.path.lower()
        if ALLOWED_SOURCE_REPOSITORY.lower() in normalized_path:
            allowed_url = urlunparse(parsed._replace(query="", fragment=""))
    if allowed_url:
        kind = "https_repository"
        label = "stock-dashboard holdings data"
    elif raw.startswith("env:"):
        kind = "environment_secret"
        label = "protected holdings input"
    elif raw:
        kind = "local_file" if Path(raw).is_absolute() or raw.startswith((".", "..")) else "configured_input"
        label = "local holdings input" if kind == "local_file" else "configured holdings input"
    else:
        kind = "unknown"
        label = "holdings input"
    return {
        "source_kind": kind,
        "source_label": label,
        "source_fingerprint": fingerprint,
        "source_link": allowed_url,
    }


def sanitize_public_holding(record: dict[str, Any]) -> dict[str, str]:
    result = {
        "account": str(record.get("account") or "").strip(),
        "type": normalize_holding_type(record.get("type")),
        "name": str(record.get("name") or "").strip(),
        "code": normalize_code(record.get("code")),
    }
    return {key: result[key] for key in PUBLIC_HOLDING_FIELDS}


def build_public_holdings_snapshot(data: dict[str, Any], source: str) -> tuple[dict[str, Any], list[str]]:
    holdings = data.get("holdings")
    if not isinstance(holdings, list):
        raise HoldingsSchemaError("holdings must be a list")

    accounts: dict[str, dict[str, list[dict[str, str]]]] = {}
    warnings: list[str] = []
    for index, record in enumerate(holdings):
        if not isinstance(record, dict):
            warnings.append(f"holding[{index}] is not an object")
            continue
        if not bool(record.get("enabled", True)):
            continue
        asset_type = normalize_holding_type(record.get("type"))
        if asset_type not in SNAPSHOT_TYPES:
            warnings.append(f"holding[{index}] has unsupported enabled type {record.get('type')!r}")
            continue
        item = sanitize_public_holding(record)
        if not item["code"]:
            warnings.append(f"holding[{index}] is missing code")
            continue
        account = item["account"] or "未分组账户"
        item["account"] = account
        groups = accounts.setdefault(account, {kind: [] for kind in SNAPSHOT_TYPES})
        if not any(existing["code"] == item["code"] for existing in groups[asset_type]):
            groups[asset_type].append(item)

    source_meta = public_source_descriptor(source, data)
    snapshot = {
        "schema_version": HOLDINGS_SCHEMA_VERSION,
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        **source_meta,
        "accounts": accounts,
        "type_labels": TYPE_LABELS,
        "validation_warnings": warnings,
    }
    return snapshot, warnings


def stock_items_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    accounts = snapshot.get("accounts")
    if not isinstance(accounts, dict):
        raise HoldingsSchemaError("public snapshot accounts must be an object")
    for account_name, groups in accounts.items():
        if not isinstance(groups, dict):
            raise HoldingsSchemaError(f"account {account_name!r} groups must be an object")
        for holding in groups.get("stock", []):
            if not isinstance(holding, dict):
                continue
            code = normalize_code(holding.get("code"))
            if not code:
                continue
            item = by_code.setdefault(
                code,
                {
                    "code": code,
                    "name": str(holding.get("name") or code).strip() or code,
                    "type": "stock",
                    "accounts": [],
                },
            )
            account = str(holding.get("account") or account_name).strip()
            if account and account not in item["accounts"]:
                item["accounts"].append(account)
    for item in by_code.values():
        item["accounts"].sort()
    return [by_code[code] for code in sorted(by_code)]


def count_snapshot_types(snapshot: dict[str, Any]) -> dict[str, int]:
    counts = {kind: 0 for kind in SNAPSHOT_TYPES}
    for groups in (snapshot.get("accounts") or {}).values():
        if not isinstance(groups, dict):
            continue
        for kind in SNAPSHOT_TYPES:
            values = groups.get(kind)
            if isinstance(values, list):
                counts[kind] += len(values)
    return counts


def iter_public_holdings(snapshot: dict[str, Any]) -> Iterable[dict[str, str]]:
    for groups in (snapshot.get("accounts") or {}).values():
        if not isinstance(groups, dict):
            continue
        for kind in SNAPSHOT_TYPES:
            for item in groups.get(kind, []):
                if isinstance(item, dict):
                    yield sanitize_public_holding(item)
