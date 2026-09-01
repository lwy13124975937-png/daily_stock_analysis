"""Validate a complete static-site build as one immutable public artifact."""

from __future__ import annotations

import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from src.reports.contracts import (
    ADVICE_EVALUATION_VERSION,
    BUILD_MANIFEST_SCHEMA_VERSION,
    DataIntegrityError,
    SITE_RENDERER_VERSION,
    read_json_strict,
    sha256_file,
)
from src.reports.structured_stock_report import validate_structured_stock_report
from src.site.builder import _validate_holdings, _validate_input_coherence, _validate_steady


FORBIDDEN_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "math"}
FORBIDDEN_SCHEMES = {"javascript", "data", "file", "vbscript"}
SENSITIVE_KEYS = {
    "unit_cost",
    "shares",
    "cost",
    "market_value",
    "profit",
    "amount",
    "total",
    "quantity",
    "avg_cost",
    "market_value_base",
    "unrealized_pnl",
}
SENSITIVE_PHRASES = (
    "持仓成本",
    "单位成本",
    "成本价",
    "持仓份额",
    "基金份额",
    "持仓金额",
    "账户金额",
    "总金额",
    "总资产",
    "持仓市值",
    "账户市值",
    "基金市值",
    "持仓盈亏",
    "盈亏",
    "浮盈",
    "浮亏",
    "收益金额",
)
KEY_LIKE_RE = re.compile(
    r"(?is)(?:[\"'](?:unit_cost|shares|cost|market_value|profit|amount|total)[\"']\s*[:=]|"
    r"<(?:th|td)\b[^>]*>\s*(?:unit_cost|shares|cost|market_value|profit|amount|total)\s*</(?:th|td)>|"
    r"\b(?:unit_cost|market_value)\b)"
)


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.hrefs: list[str] = []
        self.build_meta: list[str] = []
        self.body_build_ids: list[str] = []
        self.summary_texts: list[str] = []
        self._in_summary = 0
        self._summary_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        tag = tag.lower()
        self.tags.append((tag, values))
        if tag in {"a", "link"} and values.get("href"):
            self.hrefs.append(str(values["href"]))
        if tag == "meta" and str(values.get("name") or "").lower() == "build-id":
            self.build_meta.append(str(values.get("content") or ""))
        if tag == "body" and values.get("data-build-id"):
            self.body_build_ids.append(str(values["data-build-id"]))
        if tag == "summary":
            self._in_summary += 1
            self._summary_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "summary" and self._in_summary:
            self._in_summary -= 1
            self.summary_texts.append("".join(self._summary_parts).strip())

    def handle_data(self, data: str) -> None:
        if self._in_summary:
            self._summary_parts.append(data)


def _strict_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataIntegrityError(f"invalid UTF-8 public file: {path}: {exc}") from exc


def _walk_keys(value: Any, *, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                errors.append(f"public JSON exposes forbidden key {path}.{key}")
            errors.extend(_walk_keys(nested, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            errors.extend(_walk_keys(nested, path=f"{path}[{index}]"))
    return errors


def _walk_sensitive_values(value: Any, *, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            errors.extend(_walk_sensitive_values(nested, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            errors.extend(_walk_sensitive_values(nested, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        for phrase in SENSITIVE_PHRASES:
            if phrase in value:
                errors.append(f"public JSON contains sensitive holding phrase {phrase!r} at {path}")
        if KEY_LIKE_RE.search(value):
            errors.append(f"public JSON contains structured sensitive field at {path}")
    return errors


def _check_page(site: Path, path: Path, build_id: str) -> list[str]:
    errors: list[str] = []
    text = _strict_text(path)
    parser = PageParser()
    try:
        parser.feed(text)
    except Exception as exc:
        return [f"invalid HTML {path.relative_to(site)}: {type(exc).__name__}: {exc}"]
    relative = path.relative_to(site).as_posix()
    if parser.build_meta != [build_id] or parser.body_build_ids != [build_id]:
        errors.append(f"build_id mismatch: {relative}")
    for tag, attrs in parser.tags:
        if tag in FORBIDDEN_TAGS:
            errors.append(f"unsafe tag <{tag}>: {relative}")
        for name, value in attrs.items():
            lowered = name.lower()
            if lowered.startswith("on") or lowered == "style":
                errors.append(f"unsafe attribute {name}: {relative}")
            if lowered in {"href", "src"} and value:
                scheme = urlparse(value.strip()).scheme.lower()
                if scheme in FORBIDDEN_SCHEMES:
                    errors.append(f"unsafe URL scheme {scheme}: {relative}")
    for href in parser.hrefs:
        parsed = urlparse(href)
        if parsed.scheme in {"https", "mailto"} or href.startswith("#"):
            continue
        if parsed.scheme:
            errors.append(f"unsupported URL scheme {parsed.scheme}: {relative}")
            continue
        target = (path.parent / parsed.path).resolve()
        if site.resolve() not in (target, *target.parents) or not target.exists():
            errors.append(f"broken internal link {href}: {relative}")
    for phrase in SENSITIVE_PHRASES:
        if phrase in text:
            errors.append(f"public page contains sensitive holding phrase {phrase!r}: {relative}")
    if KEY_LIKE_RE.search(text):
        errors.append(f"public page contains structured sensitive field: {relative}")
    if "暂无持仓快照" in text:
        errors.append(f"degraded holdings placeholder is not publishable: {relative}")
    if any(token in text for token in ("模型输出疑似截断", "组合在</", "当前组合在</", "基于当前持仓清单做</")):
        errors.append(f"truncated portfolio review is not publishable: {relative}")
    return errors


def _check_advice(payload: Mapping[str, Any], report: Mapping[str, Any], html: str) -> list[str]:
    errors: list[str] = []
    if payload.get("evaluation_version") != ADVICE_EVALUATION_VERSION:
        errors.append("advice evaluation_version missing or stale")
    if payload.get("latest_report_date") != report.get("report_date"):
        errors.append("advice latest_report_date differs from stock report")
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    current = [record for record in records if isinstance(record, Mapping) and record.get("is_current_holding_now")]
    if report.get("expected_count") and not current:
        errors.append("latest report has stocks but advice current-holding records are empty")
    if "总建议准确率" in html or re.search(r"AI\s*建议准确率\s*\d", html):
        errors.append("advice page exposes a mixed overall accuracy metric")
    for metric_name in ("方向性动作命中率", "情绪方向一致率", "持有结果", "观望区间一致率"):
        if metric_name not in html:
            errors.append(f"advice page missing metric: {metric_name}")
    summary = payload.get("summary_all_history") if isinstance(payload.get("summary_all_history"), Mapping) else {}
    if int(summary.get("total_advice") or 0) != len(records):
        errors.append("advice total_advice count mismatch")
    return errors


def _check_steady(payload: Mapping[str, Any], html: str) -> list[str]:
    errors: list[str] = []
    try:
        _validate_steady(payload)
    except ValueError as exc:
        errors.append(str(exc))
    if any(token in html for token in ("股息率对应价格", "高股息观察价", "合理买点")):
        errors.append("steady-income default page exposes a dividend-price band")
    if payload.get("data_status") == "valid_zero":
        if int(payload.get("qualified_count") or 0) != 0 or "个候选中没有满足全部硬条件的标的" not in html:
            errors.append("steady-income valid-zero state is rendered incorrectly")
    if payload.get("data_status") == "provider_unavailable":
        if "本次深度评估因数据源异常未能完成" not in html or "个候选中没有满足全部硬条件的标的" in html:
            errors.append("steady-income provider outage is rendered as a valid zero")
    unevaluated = int(payload.get("unevaluated_count") or 0)
    is_exhaustive = bool(payload.get("is_exhaustive"))
    if is_exhaustive and unevaluated:
        errors.append("steady-income exhaustive result has unevaluated candidates")
    if not is_exhaustive:
        if f"未深评 {unevaluated}" not in html or "is_exhaustive=false" not in html:
            errors.append("steady-income shortlist scope is not visible")
    if payload.get("data_status") == "degraded":
        stats = payload.get("screening_stats") if isinstance(payload.get("screening_stats"), Mapping) else {}
        requested = int(stats.get("deep_requested_count") or 0)
        completed = int(stats.get("completed_evaluation_count") or 0)
        if completed < requested and f"仅完成 {completed}/{requested}" not in html:
            errors.append("steady-income degraded completion ratio is not visible")
    as_of_raw = payload.get("as_of")
    try:
        as_of = date.fromisoformat(str(as_of_raw))
    except ValueError:
        return errors + ["steady-income as_of is invalid"]
    for item in list(payload.get("candidates") or []) + list(payload.get("excluded") or []):
        if not isinstance(item, Mapping):
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        for value in evidence.values():
            if not isinstance(value, Mapping):
                continue
            available = value.get("available_at") or value.get("announced_at")
            if available:
                try:
                    if date.fromisoformat(str(available)[:10]) > as_of:
                        errors.append(f"steady-income evidence is from the future: {item.get('code')}/{available}")
                except ValueError:
                    errors.append(f"steady-income evidence date is invalid: {item.get('code')}/{available}")
    return errors


def validate_site(site: Path) -> list[str]:
    site = site.resolve()
    errors: list[str] = []
    manifest_path = site / "data" / "build_manifest.json"
    if not manifest_path.exists():
        return ["site/data/build_manifest.json is missing"]
    manifest = read_json_strict(manifest_path)
    if not isinstance(manifest, Mapping):
        return ["build manifest must be an object"]
    if manifest.get("schema_version") != BUILD_MANIFEST_SCHEMA_VERSION:
        errors.append("build manifest schema_version is missing or unsupported")
    build_id = str(manifest.get("build_id") or "")
    if not build_id:
        errors.append("build manifest has no build_id")
    content_id = str(manifest.get("content_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", content_id):
        errors.append("build manifest content_id is missing or invalid")
    elif build_id != f"content-{content_id[:20]}":
        errors.append("build_id is not derived from content_id")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("source_fingerprint") or "")):
        errors.append("build manifest source_fingerprint is missing or invalid")
    if not isinstance(manifest.get("git_dirty"), bool):
        errors.append("build manifest git_dirty must be boolean")
    if not str(manifest.get("git_sha") or ""):
        errors.append("build manifest git_sha is missing")
    try:
        built_at = datetime.fromisoformat(str(manifest.get("built_at") or ""))
        if built_at.tzinfo is None:
            raise ValueError
    except ValueError:
        errors.append("build manifest built_at must be timezone-aware")
    if manifest.get("site_renderer_version") != SITE_RENDERER_VERSION:
        errors.append("build manifest site_renderer_version is missing or stale")
    if manifest.get("manifest_in_generated_files") is not False:
        errors.append("build manifest must explicitly exclude itself from generated_files")
    entries = manifest.get("generated_files") if isinstance(manifest.get("generated_files"), list) else []
    expected = {str(item.get("path")): str(item.get("sha256")) for item in entries if isinstance(item, Mapping)}
    actual = {
        path.relative_to(site).as_posix()
        for path in site.rglob("*")
        if path.is_file() and path.relative_to(site).as_posix() != "data/build_manifest.json"
    }
    if set(expected) != actual:
        errors.append(f"site tree differs from manifest: missing={sorted(set(expected) - actual)}, extra={sorted(actual - set(expected))}")
    for relative, digest in expected.items():
        path = site / relative
        if path.exists() and sha256_file(path) != digest:
            errors.append(f"manifest hash mismatch: {relative}")

    html_paths = sorted(site.rglob("*.html"))
    for path in html_paths:
        try:
            errors.extend(_check_page(site, path, build_id))
        except DataIntegrityError as exc:
            errors.append(str(exc))

    report_path = site / "data" / f"report_{str(manifest.get('report_date') or '').replace('-', '')}.json"
    advice_path = site / "data" / "advice_accuracy.json"
    steady_path = site / "data" / "steady_income.json"
    holdings_path = site / "data" / "holdings_snapshot.json"
    for path in (report_path, advice_path, steady_path, holdings_path):
        if not path.exists():
            errors.append(f"mandatory public dataset missing: {path.relative_to(site)}")
    if errors and any(not path.exists() for path in (report_path, advice_path, steady_path, holdings_path)):
        return errors
    report = read_json_strict(report_path)
    advice = read_json_strict(advice_path)
    steady = read_json_strict(steady_path)
    holdings = read_json_strict(holdings_path)
    for name, payload in (("report", report), ("advice", advice), ("steady", steady), ("holdings", holdings)):
        if not isinstance(payload, Mapping):
            errors.append(f"{name} public dataset must be an object")
        else:
            errors.extend(_walk_keys(payload, path=f"$.{name}"))
            errors.extend(_walk_sensitive_values(payload, path=f"$.{name}"))
    if not all(isinstance(value, Mapping) for value in (report, advice, steady, holdings)):
        return errors
    try:
        validate_structured_stock_report(report)
    except ValueError as exc:
        errors.append(f"structured report invalid: {exc}")
    try:
        _validate_holdings(holdings)
    except ValueError as exc:
        errors.append(f"holdings snapshot invalid: {exc}")
    try:
        _validate_input_coherence(report, holdings, advice, steady)
    except ValueError as exc:
        errors.append(f"cross-dataset input mismatch: {exc}")
    try:
        advice_html = _strict_text(site / "advice_backtest.html")
        steady_html = _strict_text(site / "steady_income.html")
    except DataIntegrityError as exc:
        return errors + [str(exc)]
    errors.extend(_check_advice(advice, report, advice_html))
    errors.extend(_check_steady(steady, steady_html))
    latest_href = f"reports/report_{str(report.get('report_date')).replace('-', '')}.html"
    try:
        if latest_href not in _strict_text(site / "index.html"):
            errors.append("index does not link to the structured latest report")
    except DataIntegrityError as exc:
        errors.append(str(exc))
    return errors
