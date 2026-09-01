"""Single-owner deterministic builder for the public static site."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import markdown2

from src.reports.contracts import (
    ADVICE_EVALUATION_VERSION,
    BUILD_MANIFEST_SCHEMA_VERSION,
    HOLDINGS_SCHEMA_VERSION,
    SITE_RENDERER_VERSION,
    public_advice_record,
    read_json_strict,
    read_jsonl_strict,
    sha256_file,
)
from src.reports.public_holdings import TYPE_LABELS, normalize_code
from src.reports.structured_stock_report import validate_structured_stock_report
from src.site.layout import SITE_CSS, document
from src.site.security import safe_link, sanitize_html


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REPORT_JSON_RE = re.compile(r"report_(20\d{6})\.json$")
MARKET_MD_RE = re.compile(r"market_review_(20\d{6})\.md$")
SENSITIVE_RAW_LINE_RE = re.compile(
    r"(?i)(unit_cost|market_value|shares|(?:^|[\"'<\s])(?:cost|profit|amount|total)(?:[\"'>\s:=,]|$)|"
    r"持仓成本|单位成本|成本价|持仓份额|基金份额|持仓金额|账户金额|总金额|总资产|"
    r"持仓市值|账户市值|基金市值|持仓盈亏|盈亏|浮盈|浮亏|收益金额)"
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _account_slug(account: str) -> str:
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()[:10]
    return f"account-{digest}.html"


def _date_key(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))[:8]


def _strict_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 text: {path}: {exc}") from exc


def _legacy_markdown_html(text: str) -> str:
    rendered = markdown2.markdown(
        text,
        extras=["tables", "fenced-code-blocks", "break-on-newline", "cuddled-lists"],
    )
    return sanitize_html(rendered)


def _public_raw_markdown(text: str) -> str:
    lines = [line for line in text.splitlines() if not SENSITIVE_RAW_LINE_RE.search(line)]
    return _legacy_markdown_html("\n".join(lines))


def _latest_structured_report(reports_dir: Path) -> tuple[Path, dict[str, Any]]:
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in reports_dir.glob("report_*.json"):
        if not REPORT_JSON_RE.fullmatch(path.name):
            continue
        payload = read_json_strict(path)
        if not isinstance(payload, dict):
            raise ValueError(f"structured report is not an object: {path}")
        validate_structured_stock_report(payload)
        candidates.append((str(payload["report_date"]), path, payload))
    if not candidates:
        raise FileNotFoundError("no structured reports/report_YYYYMMDD.json found")
    _, path, payload = max(candidates, key=lambda item: item[0])
    return path, payload


def _validate_holdings(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("schema_version") != HOLDINGS_SCHEMA_VERSION:
        raise ValueError("holdings snapshot schema_version is missing or unsupported")
    accounts = snapshot.get("accounts")
    if not isinstance(accounts, Mapping) or not accounts:
        raise ValueError("holdings snapshot has no accounts")
    allowed_item_keys = {"account", "type", "name", "code"}
    for account, groups in accounts.items():
        if not str(account).strip() or not isinstance(groups, Mapping):
            raise ValueError("holdings snapshot account entry is malformed")
        for asset_type, items in groups.items():
            if asset_type not in {"stock", "lof", "otc"}:
                raise ValueError(f"unsupported public holding type: {asset_type}")
            if not isinstance(items, list):
                raise ValueError(f"holdings snapshot {account}/{asset_type} must be a list")
            for item in items:
                if not isinstance(item, Mapping) or set(item) - allowed_item_keys:
                    raise ValueError(f"holdings snapshot exposes unexpected fields: {account}/{asset_type}")


def _validate_portfolio_review_coverage(
    report: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> None:
    expected: dict[tuple[str, str], set[str]] = {}
    accounts = snapshot.get("accounts") if isinstance(snapshot.get("accounts"), Mapping) else {}
    for account, groups in accounts.items():
        if not isinstance(groups, Mapping):
            continue
        for asset_type in ("lof", "otc"):
            holdings = groups.get(asset_type) if isinstance(groups.get(asset_type), list) else []
            codes = {
                str(item.get("code") or "").strip()
                for item in holdings
                if isinstance(item, Mapping) and str(item.get("code") or "").strip()
            }
            if codes:
                expected[(str(account), asset_type)] = codes

    actual: dict[tuple[str, str], set[str]] = {}
    for review in report.get("portfolio_reviews", []):
        if not isinstance(review, Mapping):
            continue
        key = (str(review.get("account") or ""), str(review.get("asset_type") or ""))
        if key in actual:
            raise ValueError(f"duplicate portfolio review for {key[0]}/{key[1]}")
        actual[key] = {
            str(item.get("code") or "").strip()
            for item in review.get("holdings", [])
            if isinstance(item, Mapping) and str(item.get("code") or "").strip()
        }
    if expected != actual:
        raise ValueError(
            "portfolio review coverage differs from holdings snapshot: "
            f"expected={sorted(expected)} actual={sorted(actual)}"
        )


def _snapshot_stock_codes(snapshot: Mapping[str, Any]) -> set[str]:
    codes: set[str] = set()
    accounts = snapshot.get("accounts") if isinstance(snapshot.get("accounts"), Mapping) else {}
    for groups in accounts.values():
        if not isinstance(groups, Mapping):
            continue
        for item in groups.get("stock", []):
            if isinstance(item, Mapping) and normalize_code(item.get("code")):
                codes.add(normalize_code(item.get("code")))
    return codes


def _validate_input_coherence(
    report: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    advice: Mapping[str, Any],
    steady: Mapping[str, Any],
) -> None:
    report_date = str(report.get("report_date") or "")
    expected_codes = {normalize_code(value) for value in report.get("expected_stock_codes", [])}
    snapshot_codes = _snapshot_stock_codes(snapshot)
    if expected_codes != snapshot_codes:
        raise ValueError(
            "stock report and holdings snapshot identities differ: "
            f"report={sorted(expected_codes)} snapshot={sorted(snapshot_codes)}"
        )
    snapshot_generated = str(snapshot.get("generated_at") or "")[:10]
    if snapshot_generated != report_date:
        raise ValueError(
            f"holdings snapshot date {snapshot_generated!r} differs from report date {report_date!r}"
        )
    if str(steady.get("as_of") or "") != report_date:
        raise ValueError("steady-income as_of differs from stock report date")
    if advice.get("latest_report_date") != report_date:
        raise ValueError("advice dataset and latest stock report date do not match")
    records = advice.get("records") if isinstance(advice.get("records"), list) else []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("advice record must be an object")
        expected_current = normalize_code(record.get("code")) in snapshot_codes
        if bool(record.get("is_current_holding_now")) != expected_current:
            raise ValueError(
                f"advice current-holding flag differs from snapshot: {record.get('code')}"
            )
    current_count = sum(
        isinstance(record, Mapping) and bool(record.get("is_current_holding_now"))
        for record in records
    )
    summary = advice.get("summary_current_holdings")
    if not isinstance(summary, Mapping) or int(summary.get("total_advice") or 0) != current_count:
        raise ValueError("advice current-holdings summary count mismatch")
    _validate_portfolio_review_coverage(report, snapshot)


def _holding_counts(groups: Mapping[str, Any]) -> dict[str, int]:
    return {key: len(groups.get(key) or []) for key in ("stock", "lof", "otc")}


def _badge(text: str, kind: str = "") -> str:
    return f'<span class="badge {escape(kind)}">{escape(text)}</span>'


def _stock_result_line(result: Mapping[str, Any]) -> str:
    if not result.get("success"):
        return str(result.get("public_message") or "分析失败，本标的未完成分析。")
    bits = [str(result.get("action_raw") or "未分类")]
    if result.get("score") is not None:
        bits.append(f"评分 {result['score']}")
    if result.get("sentiment_raw"):
        bits.append(str(result["sentiment_raw"]))
    return "｜".join(bits)


def _render_stock(item: Mapping[str, Any], result: Mapping[str, Any] | None) -> str:
    code = normalize_code(item.get("code"))
    name = str(item.get("name") or code)
    if result is None:
        result = {"success": False, "public_message": "分析结果缺失，本标的未完成分析。", "sections": {}}
    line = _stock_result_line(result)
    if not result.get("success"):
        return (
            '<article class="stock-row"><div class="stock-body">'
            f'<div class="stock-head"><strong>{escape(name)}（{escape(code)}）</strong>{_badge("未完成", "bad")}</div>'
            f'<p>{escape(line)}</p></div></article>'
        )
    sections = result.get("sections") if isinstance(result.get("sections"), Mapping) else {}
    conclusion = str(sections.get("core_conclusion") or result.get("public_summary") or "暂无核心结论。")
    plans = sections.get("battle_plan") if isinstance(sections.get("battle_plan"), list) else []
    related = str(sections.get("related_sector") or "")
    detail_blocks = []
    for key, title in (
        ("trend_analysis", "趋势"),
        ("technical_analysis", "技术面"),
        ("fundamental_analysis", "基本面"),
        ("risk_warning", "风险"),
    ):
        value = str(sections.get(key) or "").strip()
        if value:
            detail_blocks.append(f"<h4>{title}</h4><p>{escape(value)}</p>")
    plan_html = "".join(f"<li>{escape(str(value))}</li>" for value in plans[:3])
    related_html = f'<p class="muted">关联板块：{escape(related)}</p>' if related else ""
    return f"""
<details class="stock-row">
  <summary><span class="stock-head"><span>{escape(name)}（{escape(code)}）</span><span>{escape(line)}</span></span></summary>
  <div class="stock-body">
    <p class="conclusion">{escape(conclusion)}</p>
    {f'<h4>作战计划</h4><ul>{plan_html}</ul>' if plan_html else ''}
    {related_html}
    {''.join(detail_blocks)}
  </div>
</details>"""


def _render_fund_list(asset_type: str, items: list[Mapping[str, Any]]) -> str:
    if not items:
        return ""
    rule = (
        "以下标的统一纳入账户级 LOF/ETF 组合复盘，不进行单只短线判断。"
        if asset_type == "lof"
        else "以下基金统一纳入账户级场外基金组合复盘，不进行单只短线判断。"
    )
    rows = "".join(
        f'<li class="holding-line"><span>{escape(str(item.get("name") or item.get("code") or ""))}</span>'
        f'<span>{escape(normalize_code(item.get("code")))}</span></li>'
        for item in items
    )
    return f'<div class="panel"><h3>{escape(TYPE_LABELS[asset_type])}</h3><p class="muted">{rule}</p><ul class="holding-list">{rows}</ul></div>'


def _render_review(review: Mapping[str, Any]) -> str:
    asset_type = str(review.get("asset_type") or "")
    title = "LOF/ETF 组合复盘" if asset_type == "lof" else "场外基金组合复盘"
    status = str(review.get("status") or "")
    badge = _badge("AI 复盘" if status == "ai" else "规则兜底", "good" if status == "ai" else "warn")
    sections = review.get("sections") if isinstance(review.get("sections"), Mapping) else {}
    order = ("组合观察", "风格暴露", "配置节奏", "后续观察")
    blocks = []
    for heading in order:
        values = sections.get(heading)
        if not isinstance(values, list):
            continue
        lines = "".join(f"<li>{escape(str(value))}</li>" for value in values if str(value).strip())
        if lines:
            blocks.append(f"<section><h4>{escape(heading)}</h4><ul>{lines}</ul></section>")
    return f'<article class="review"><div class="status-line"><h3>{title}</h3>{badge}</div>{"".join(blocks)}</article>'


def _render_account(
    account: str,
    groups: Mapping[str, Any],
    *,
    results_by_code: Mapping[str, Mapping[str, Any]],
    reviews_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    counts = _holding_counts(groups)
    summary = "".join(
        f'<span class="pill">{escape(TYPE_LABELS[key])} {counts[key]}</span>'
        for key in ("stock", "lof", "otc")
        if counts[key]
    )
    stocks = groups.get("stock") if isinstance(groups.get("stock"), list) else []
    stock_html = "".join(
        _render_stock(item, results_by_code.get(normalize_code(item.get("code"))))
        for item in stocks
        if isinstance(item, Mapping)
    )
    fund_parts = []
    for asset_type in ("lof", "otc"):
        items = groups.get(asset_type) if isinstance(groups.get(asset_type), list) else []
        if items:
            fund_parts.append(_render_fund_list(asset_type, items))
            review = reviews_by_key.get((account, asset_type))
            if review:
                fund_parts.append(_render_review(review))
            else:
                raise ValueError(f"missing structured portfolio review for {account}/{asset_type}")
    return f"""
<section class="section account-section" data-account="{escape(account, quote=True)}">
  <div class="section-head"><div><h2>{escape(account)}</h2><div class="account-summary">{summary}</div></div></div>
  {f'<div class="panel"><h3>A 股逐项分析</h3>{stock_html}</div>' if stocks else ''}
  {''.join(fund_parts)}
</section>"""


def _render_stock_report(
    report: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    raw_markdown: str,
    *,
    build_id: str,
) -> str:
    results = report.get("results") if isinstance(report.get("results"), list) else []
    results_by_code = {normalize_code(item.get("code")): item for item in results if isinstance(item, Mapping)}
    reviews = report.get("portfolio_reviews") if isinstance(report.get("portfolio_reviews"), list) else []
    reviews_by_key = {
        (str(item.get("account") or ""), str(item.get("asset_type") or "")): item
        for item in reviews
        if isinstance(item, Mapping)
    }
    accounts = snapshot.get("accounts") if isinstance(snapshot.get("accounts"), Mapping) else {}
    account_html = "".join(
        _render_account(
            str(account),
            groups,
            results_by_code=results_by_code,
            reviews_by_key=reviews_by_key,
        )
        for account, groups in accounts.items()
        if isinstance(groups, Mapping)
    )
    status = str(report.get("status") or "unknown")
    date_text = str(report.get("report_date") or "")
    raw = _public_raw_markdown(raw_markdown)
    body = f"""
<nav class="nav"><a href="../index.html">返回首页</a><a href="index.html">报告归档</a></nav>
<header class="hero"><span class="kicker">结构化持仓日报</span><h1>{escape(date_text)} 持仓日报</h1>
<div class="meta"><span>状态：{escape(status)}</span><span>成功 {int(report.get('success_count') or 0)}</span><span>失败 {int(report.get('failure_count') or 0)}</span><span>锚点交易日：{escape(str(report.get('anchor_session') or ''))}</span></div></header>
{account_html}
<details class="archive"><summary>原始 AI 股票日报（审计留档）</summary><div class="archive-body raw-fragment">{raw}</div></details>
<footer class="footer">AI 内容仅作复盘参考，不构成投资建议。</footer>"""
    return document(f"{date_text} 持仓日报", body, build_id=build_id, asset_prefix="../")


def _render_account_page(
    account: str,
    groups: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    build_id: str,
) -> str:
    results = report.get("results") if isinstance(report.get("results"), list) else []
    results_by_code = {normalize_code(item.get("code")): item for item in results if isinstance(item, Mapping)}
    reviews = report.get("portfolio_reviews") if isinstance(report.get("portfolio_reviews"), list) else []
    reviews_by_key = {
        (str(item.get("account") or ""), str(item.get("asset_type") or "")): item
        for item in reviews
        if isinstance(item, Mapping)
    }
    body = (
        '<nav class="nav"><a href="../index.html">返回首页</a></nav>'
        + _render_account(account, groups, results_by_code=results_by_code, reviews_by_key=reviews_by_key)
        + '<footer class="footer">账户页与总日报使用同一结构化渲染结果。</footer>'
    )
    return document(f"{account}持仓复盘", body, build_id=build_id, asset_prefix="../")


def _format_rate(value: Any) -> str:
    return "样本不足" if value is None else f"{float(value) * 100:.1f}%"


def _format_return(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.2f}%"


def _public_accuracy(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed_top = {
        "schema_version",
        "evaluation_version",
        "updated_at",
        "latest_report_date",
        "latest_report_name",
        "new_advice_count",
        "new_advice_message",
        "neutral_band",
        "summary_all_history",
        "summary_current_holdings",
        "metrics_all_history",
        "metrics_current_holdings",
        "migration_stats",
        "history_source_status",
        "previous_history_count",
    }
    result = {key: payload.get(key) for key in allowed_top if key in payload}
    for key in ("records", "recent_records"):
        values = payload.get(key) if isinstance(payload.get(key), list) else []
        result[key] = [public_advice_record(item) for item in values if isinstance(item, Mapping)]
    miss_values = payload.get("miss_cases") if isinstance(payload.get("miss_cases"), list) else []
    result["miss_cases"] = [
        {
            key: item.get(key)
            for key in (
                "date",
                "code",
                "name",
                "accounts",
                "action_raw",
                "action_normalized",
                "sentiment_raw",
                "period",
                "return",
                "miss_reason",
                "is_current_holding_now",
            )
            if key in item
        }
        for item in miss_values
        if isinstance(item, Mapping)
    ]
    return result


def _render_metric_card(title: str, metric: Mapping[str, Any], *, kind: str) -> str:
    rows = []
    for period in ("d1", "d5", "d20"):
        sample = int(metric.get(f"{period}_sample_size") or 0)
        if kind == "hold":
            value = _format_return(metric.get(f"{period}_median_return")) if sample else "样本不足"
            detail = f"中位收益 {value}；明显回撤 {int(metric.get(f'{period}_material_drawdown_count') or 0)}"
        else:
            detail = f"{_format_rate(metric.get(f'{period}_rate'))}（n={sample}）"
        rows.append(f"<p>{period.upper()}：{escape(detail)}</p>")
    return f'<article class="metric-card"><h3>{escape(title)}</h3><p>样本 {int(metric.get("total_advice") or 0)}</p>{"".join(rows)}</article>'


def _record_status(record: Mapping[str, Any], period: str) -> str:
    status = str(record.get(f"{period}_status") or "等待验证")
    if status != "已验证":
        return status
    action = str(record.get("action_normalized") or "unknown")
    if action in {"buy", "increase", "sell", "reduce"}:
        return "方向命中" if record.get(f"{period}_direction_hit") is True else "方向未命中"
    if action == "observe":
        return "区间一致" if record.get(f"{period}_observe_consistent") is True else "区间不一致"
    if action in {"hold", "hold_watch"}:
        return "明显回撤" if record.get(f"{period}_hold_drawdown_flag") else "结果已记录"
    return "已验证"


def _render_record(record: Mapping[str, Any]) -> str:
    accounts = " / ".join(str(value) for value in record.get("accounts", []) if str(value).strip())
    current = "当前仍持有" if record.get("is_current_holding_now") else "历史持仓"
    periods = "".join(
        f'<span class="period"><small>{period.upper()}</small><strong>{escape(_record_status(record, period))}</strong>'
        f'<em>{escape(_format_return(record.get(f"{period}_return")))}</em></span>'
        for period in ("d1", "d5", "d20")
    )
    return f"""
<article class="record"><div class="record-head"><strong>{escape(str(record.get('name') or ''))}（{escape(str(record.get('code') or ''))}）</strong>{_badge(current)}</div>
<div class="meta"><span>{escape(str(record.get('date') or ''))}</span><span>{escape(accounts)}</span></div>
<p>{escape(str(record.get('action_raw') or 'unknown'))}｜{escape(str(record.get('sentiment_raw') or 'unknown'))}</p><div class="periods">{periods}</div></article>"""


def _render_advice(payload: Mapping[str, Any], *, build_id: str) -> str:
    metrics = payload.get("metrics_all_history") if isinstance(payload.get("metrics_all_history"), Mapping) else {}
    metric_cards = "".join(
        (
            _render_metric_card("方向性动作命中率", metrics.get("directional_action", {}), kind="binary"),
            _render_metric_card("情绪方向一致率", metrics.get("sentiment_alignment", {}), kind="binary"),
            _render_metric_card("持有结果", metrics.get("hold_results", {}), kind="hold"),
            _render_metric_card("观望区间一致率", metrics.get("observe_consistency", {}), kind="binary"),
        )
    )
    actions = metrics.get("action_distribution") if isinstance(metrics.get("action_distribution"), Mapping) else {}
    directional_count = sum(int(actions.get(key) or 0) for key in ("buy", "increase", "sell", "reduce"))
    buy_count = sum(int(actions.get(key) or 0) for key in ("buy", "increase"))
    sample_note = (
        "当前没有历史买入/加仓样本，方向性动作样本主要来自卖出/减仓，不能代表完整双向能力。"
        if directional_count and not buy_count
        else "各项统计彼此独立，动作与情绪不会互相改写。"
    )
    recent = payload.get("recent_records") if isinstance(payload.get("recent_records"), list) else []
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    current = [item for item in records if isinstance(item, Mapping) and item.get("is_current_holding_now")]
    misses = payload.get("miss_cases") if isinstance(payload.get("miss_cases"), list) else []
    recent_html = "".join(_render_record(item) for item in recent if isinstance(item, Mapping)) or '<p class="muted">暂无记录。</p>'
    current_html = "".join(_render_record(item) for item in current[-20:] if isinstance(item, Mapping)) or '<p class="muted">暂无当前持仓历史样本。</p>'
    miss_html = "".join(_render_record(item) for item in misses[:10] if isinstance(item, Mapping)) or '<p class="muted">暂无可展示的偏差样本。</p>'
    rows = "".join(
        f'<tr><td data-label="日期">{escape(str(item.get("date") or ""))}</td><td data-label="标的">{escape(str(item.get("name") or ""))}（{escape(str(item.get("code") or ""))}）</td><td data-label="动作">{escape(str(item.get("action_raw") or ""))}</td><td data-label="情绪">{escape(str(item.get("sentiment_raw") or ""))}</td><td data-label="T+1">{escape(_record_status(item, "d1"))}</td></tr>'
        for item in records
        if isinstance(item, Mapping)
    )
    body = f"""
<nav class="nav"><a href="index.html">返回首页</a></nav>
<header class="hero"><span class="kicker">规则评估，不调用 LLM</span><h1>AI 历史建议复盘</h1>
<p class="lead">动作、持有、观望和情绪采用四套独立口径；不再展示一个混合且误导的“总准确率”。</p>
<div class="meta"><span>评估版本：{escape(str(payload.get('evaluation_version') or ''))}</span><span>最新读取日报：{escape(str(payload.get('latest_report_date') or ''))}</span><span>{escape(str(payload.get('new_advice_message') or ''))}</span></div></header>
<section class="section"><h2>分项统计</h2><div class="metric-grid">{metric_cards}</div><div class="note">{escape(sample_note)}</div></section>
<section class="section"><h2>当前持仓建议回看</h2><div class="record-list">{current_html}</div></section>
<section class="section"><h2>最近建议</h2><div class="record-list">{recent_html}</div></section>
<details class="collection"><summary>查看近期偏差样本</summary><div class="details-body record-list">{miss_html}</div></details>
<details class="collection"><summary>查看完整历史（{len(records)} 条）</summary><div class="details-body"><table class="compact-table"><thead><tr><th>日期</th><th>标的</th><th>动作</th><th>情绪</th><th>T+1</th></tr></thead><tbody>{rows}</tbody></table></div></details>
<footer class="footer">本页面用于回看历史建议与后续真实行情的一致性，不构成投资建议。</footer>"""
    return document("AI 历史建议复盘", body, build_id=build_id)


def _render_steady_item(item: Mapping[str, Any], *, compact: bool = False) -> str:
    name = str(item.get("name") or item.get("code") or "")
    code = str(item.get("code") or "")
    label = str(item.get("public_risk_label") or item.get("risk_tier") or "数据不足")
    reasons = item.get("risks") if isinstance(item.get("risks"), list) else []
    if compact:
        reason = str(reasons[0] if reasons else item.get("data_status") or "未通过规则证据门槛")
        return f'<li class="holding-line"><span>{escape(name)}（{escape(code)}）</span><span>{escape(label)} · {escape(reason)}</span></li>'
    evidence = "".join(
        f'<div class="evidence"><span>{escape(title)}</span><strong>{escape(value)}</strong></div>'
        for title, value in (
            ("最近有效价格", f"{item.get('current_price') or '-'} · {item.get('price_date') or '-'}"),
            ("TTM 税前股息率", f"{item.get('ttm_dividend_yield_pct') or '-'}%"),
            ("连续已实施分红", f"{int(item.get('consecutive_dividend_years') or 0)} 年"),
            ("近年最大回撤", f"{item.get('max_drawdown_pct') or '-'}%"),
            ("年化波动率", f"{item.get('annualized_volatility_pct') or '-'}%"),
            ("行业模型", str(item.get("sector_model") or "unknown")),
        )
    )
    risk_html = "".join(f"<li>{escape(str(value))}</li>" for value in reasons) or "<li>未发现规则排除项。</li>"
    return f'<article class="steady-card"><div class="status-line"><h3>{escape(name)}（{escape(code)}）</h3>{_badge(label, "good")}</div><div class="evidence-grid">{evidence}</div><h4>风险与证据边界</h4><ul>{risk_html}</ul></article>'


def _validate_steady(payload: Mapping[str, Any]) -> None:
    for key in (
        "schema_version",
        "model_version",
        "evaluator_version",
        "ruleset_version",
        "sector_model_version",
        "evidence_version",
        "price_model_version",
        "as_of",
        "data_status",
        "selection_mode",
    ):
        if not payload.get(key):
            raise ValueError(f"steady-income payload missing {key}")
    stats = payload.get("screening_stats")
    if not isinstance(stats, Mapping):
        raise ValueError("steady-income screening_stats missing")
    universe = int(stats.get("universe_count") or 0)
    prefilter = int(stats.get("prefilter_eligible_count") or 0)
    deep = int(payload.get("deep_evaluated_count") or payload.get("evaluated_count") or 0)
    unevaluated = int(payload.get("unevaluated_count") or 0)
    qualified = int(payload.get("qualified_count") or 0)
    if not (universe >= prefilter >= deep >= qualified >= 0):
        raise ValueError("steady-income screening counts are inconsistent")
    if unevaluated != prefilter - deep:
        raise ValueError("steady-income unevaluated_count mismatch")
    is_exhaustive = bool(payload.get("is_exhaustive"))
    if is_exhaustive != (unevaluated == 0):
        raise ValueError("steady-income is_exhaustive mismatch")
    selection_mode = str(payload.get("selection_mode") or "")
    if selection_mode not in {"fixed_shortlist", "adaptive_shortlist", "exhaustive"}:
        raise ValueError("steady-income selection_mode is invalid")
    if selection_mode == "exhaustive" and not is_exhaustive:
        raise ValueError("steady-income exhaustive mode leaves candidates unevaluated")
    if int(payload.get("universe_count") or 0) != universe:
        raise ValueError("steady-income universe_count mismatch")
    if int(payload.get("prefilter_count") or 0) != prefilter:
        raise ValueError("steady-income prefilter_count mismatch")
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    if len(candidates) != qualified:
        raise ValueError("steady-income qualified_count mismatch")
    terminal = stats.get("terminal_status_distribution")
    if not isinstance(terminal, Mapping):
        raise ValueError("steady-income terminal_status_distribution missing")
    terminal_total = sum(int(terminal.get(key) or 0) for key in (
        "evaluated_qualified",
        "evaluated_rejected",
        "insufficient_evidence",
        "unsupported_sector_model",
        "provider_failure",
        "internal_error",
    ))
    requested = int(stats.get("deep_requested_count") or 0)
    completed = int(stats.get("completed_evaluation_count") or 0)
    if terminal_total != requested or requested != deep:
        raise ValueError("steady-income terminal status counts are inconsistent")
    if completed != int(terminal.get("evaluated_qualified") or 0) + int(
        terminal.get("evaluated_rejected") or 0
    ):
        raise ValueError("steady-income completed evaluation count is inconsistent")
    if int(stats.get("success_count") or 0) != completed:
        raise ValueError("steady-income success_count must mean completed evaluation")
    for item in candidates:
        if not isinstance(item, Mapping) or not item.get("qualified"):
            raise ValueError("steady-income candidate bypasses qualification contract")
        if item.get("sector_model") != "normal_corporate":
            raise ValueError("unsupported financial sector cannot be a qualified candidate")
        if item.get("ranking_score") is None:
            raise ValueError("qualified candidate must have a comparable ranking_score")


def _render_steady(payload: Mapping[str, Any], *, build_id: str) -> str:
    _validate_steady(payload)
    stats = payload["screening_stats"]
    methodology = payload.get("methodology") if isinstance(payload.get("methodology"), Mapping) else {}
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    excluded = payload.get("excluded") if isinstance(payload.get("excluded"), list) else []
    data_status = str(payload.get("data_status") or "")
    requested = int(stats.get("deep_requested_count") or payload.get("evaluated_count") or 0)
    completed = int(stats.get("completed_evaluation_count") or 0)
    unevaluated = int(payload.get("unevaluated_count") or 0)
    is_exhaustive = bool(payload.get("is_exhaustive"))
    selection_mode = str(payload.get("selection_mode") or "")
    shortlist_scope = (
        "全部预筛候选"
        if is_exhaustive
        else f"固定 shortlist（已深评 {requested}，未深评 {unevaluated}）"
    )
    candidate_html = "".join(_render_steady_item(item) for item in candidates if isinstance(item, Mapping))
    if not candidate_html:
        if data_status == "valid_zero":
            candidate_html = (
                f'<div class="note"><strong>本次已深度评估的 {requested} 个候选中没有满足全部硬条件的标的。</strong>'
                f'<p>{"全部预筛候选均已评估。" if is_exhaustive else f"仍有 {unevaluated} 个预筛候选未做深度评估，不能据此宣称全市场无合格标的。"}</p></div>'
            )
        elif data_status == "provider_unavailable":
            candidate_html = '<div class="note"><strong>本次深度评估因数据源异常未能完成。</strong><p>当前不能得出“无合格标的”的结论，请等待数据源恢复后重新评估。</p></div>'
        else:
            candidate_html = f'<div class="note"><strong>本次深度评估仅完成 {completed}/{requested}。</strong><p>结果处于降级状态，不能据此宣称全量筛选没有合格标的。</p></div>'
    excluded_html = "".join(_render_steady_item(item, compact=True) for item in excluded if isinstance(item, Mapping))
    funnel = "".join(
        f'<div class="metric"><strong>{int(value)}</strong><span>{escape(label)}</span></div>'
        for label, value in (
            ("全市场覆盖", stats.get("universe_count") or 0),
            ("基础预筛", stats.get("prefilter_eligible_count") or 0),
            ("请求深度评估", requested),
            ("完成深度评估", completed),
            ("未深度评估", unevaluated),
            ("规则合格", payload.get("qualified_count") or 0),
        )
    )
    rejected = stats.get("rejected_by_reason") if isinstance(stats.get("rejected_by_reason"), Mapping) else {}
    rejection_text = "；".join(f"{key} {value}" for key, value in sorted(rejected.items())) or "无"
    body = f"""
<nav class="nav"><a href="index.html">返回首页</a></nav>
<header class="hero"><span class="kicker">沪深全市场预筛 · {escape(shortlist_scope)}</span><h1>稳健收益</h1>
<p class="lead">先排除证据不足、高波动和分红不可验证的标的，再在可比较的普通企业集合中排序。金融行业没有可靠专用证据时直接标为数据不足。</p>
<div class="meta"><span>基准日：{escape(str(payload.get('as_of') or ''))}</span><span>规则版本：{escape(str(payload.get('ruleset_version') or ''))}</span><span>模型版本：{escape(str(payload.get('model_version') or ''))}</span></div><div class="metrics">{funnel}</div></header>
<section class="section"><h2>{"全预筛集合规则低风险候选" if is_exhaustive else "已深度评估 shortlist 中的规则低风险候选"}</h2><div class="steady-list">{candidate_html}</div></section>
<details class="collection"><summary>查看排除/数据不足标的（{len(excluded)}）</summary><div class="details-body"><ul class="compact-list">{excluded_html or '<li>无</li>'}</ul></div></details>
<section class="panel"><h2>筛选漏斗说明</h2><p>{escape(str(methodology.get('priority') or '风险硬门槛优先，规则排序仅在可比较集合内进行。'))}</p><p>选择模式：{escape(selection_mode)}。全部证券先做基础预筛；本次深度评估预算为 {int(payload.get('deep_budget') or payload.get('evaluated_count') or 0)}，实际评估 {requested}，未评估 {unevaluated}，is_exhaustive={str(is_exhaustive).lower()}。</p><p>主要排除原因：{escape(rejection_text)}</p><p>历史行情口径：{escape(str(methodology.get('replay') or methodology.get('history') or '历史复权价格回放'))}</p></section>
<footer class="footer">规则筛选不保证本金或收益，不构成投资建议。</footer>"""
    return document("稳健收益", body, build_id=build_id)


def _render_market_page(path: Path, *, build_id: str) -> str:
    text = _strict_text(path)
    title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), path.stem)
    body = f'<nav class="nav"><a href="../index.html">返回首页</a><a href="index.html">报告归档</a></nav><header class="hero"><h1>{escape(title)}</h1></header><main class="panel raw-fragment">{_public_raw_markdown(text)}</main>'
    return document(title, body, build_id=build_id, asset_prefix="../")


def _render_index(
    report: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    market_pages: list[tuple[str, str]],
    build_id: str,
    generated_at: datetime,
) -> str:
    report_date = str(report.get("report_date") or "")
    date_key = _date_key(report_date)
    cards = [
        (f"reports/report_{date_key}.html", "最新持仓日报", f"{report_date} 持仓日报"),
        ("advice_backtest.html", "模型表现", "AI 历史建议复盘"),
        ("steady_income.html", "沪深全市场", "稳健收益"),
    ]
    if market_pages:
        market_date, market_href = max(market_pages)
        cards.insert(1, (market_href, "大盘复盘", f"{market_date} 大盘复盘"))
    report_cards = "".join(
        f'<a class="card report-card" href="{escape(href, quote=True)}"><span class="kicker">{escape(kicker)}</span><strong>{escape(title)}</strong><span class="action">查看 →</span></a>'
        for href, kicker, title in cards
    )
    accounts = snapshot.get("accounts") if isinstance(snapshot.get("accounts"), Mapping) else {}
    account_cards = []
    for account, groups in accounts.items():
        if not isinstance(groups, Mapping):
            continue
        counts = _holding_counts(groups)
        count_html = "".join(
            f'<span class="count"><strong>{counts[key]}</strong><span>{escape(TYPE_LABELS[key])}</span></span>'
            for key in ("stock", "lof", "otc")
        )
        account_cards.append(
            f'<a class="card" href="accounts/{_account_slug(str(account))}"><div class="account-head"><strong>{escape(str(account))}</strong><span>→</span></div><div class="counts">{count_html}</div></a>'
        )
    source_label = str(snapshot.get("source_label") or snapshot.get("source_kind") or "持仓数据")
    source = safe_link(snapshot.get("source_link"), source_label)
    body = f"""
<header class="hero"><span class="kicker">同一构建事务 · 每日更新</span><h1>每日持仓复盘</h1><p class="lead">持仓分析、基金账户复盘、历史建议评估与沪深全市场稳健收益筛选。</p><div class="meta"><span>构建时间：{escape(generated_at.isoformat(timespec='seconds'))}</span><span>持仓快照：{escape(str(snapshot.get('generated_at') or ''))}</span><span>数据来源：{source}</span></div></header>
<section class="section"><h2>报告中心</h2><div class="grid">{report_cards}</div></section>
<section class="section"><h2>账户入口</h2><div class="grid">{''.join(account_cards)}</div></section>
<footer class="footer">公开页面仅包含经白名单审核的非敏感字段。</footer>"""
    return document("每日持仓复盘", body, build_id=build_id)


def _render_archive(report_date: str, market_pages: list[tuple[str, str]], *, build_id: str) -> str:
    rows = [f'<li><a href="report_{_date_key(report_date)}.html">{escape(report_date)} 持仓日报</a></li>']
    rows.extend(f'<li><a href="{escape(Path(href).name, quote=True)}">{escape(day)} 大盘复盘</a></li>' for day, href in sorted(market_pages, reverse=True))
    body = f'<nav class="nav"><a href="../index.html">返回首页</a></nav><header class="hero"><h1>报告归档</h1></header><section class="panel"><ul>{"".join(rows)}</ul></section>'
    return document("报告归档", body, build_id=build_id, asset_prefix="../")


def _git_sha(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


_SOURCE_IDENTITY_DIRS = (
    "src/site",
    "src/reports",
)
_SOURCE_IDENTITY_FILES = (
    "scripts/build_pages_report.py",
    "scripts/check_report_html.py",
    "requirements.txt",
    "requirements.lock",
)


def _source_identity_files(root: Path) -> list[Path]:
    """Return the exact local source inputs that own public-site rendering."""

    files: set[Path] = set()
    for relative in _SOURCE_IDENTITY_DIRS:
        base = root / relative
        if not base.exists():
            continue
        files.update(
            path
            for path in base.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    for relative in _SOURCE_IDENTITY_FILES:
        path = root / relative
        if path.is_file():
            files.add(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def source_fingerprint(root: Path) -> str:
    """Hash participating working-tree source bytes without touching git state."""

    root = root.resolve()
    digest = hashlib.sha256()
    for path in _source_identity_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _git_dirty(root: Path) -> bool:
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(output.strip())


def _content_timestamp(report: Mapping[str, Any]) -> datetime:
    try:
        value = datetime.fromisoformat(str(report.get("generated_at") or ""))
    except ValueError as exc:
        raise ValueError("structured report generated_at is invalid") from exc
    if value.tzinfo is None:
        raise ValueError("structured report generated_at must be timezone-aware")
    return value.astimezone(SHANGHAI_TZ)


def _manifest_file_entries(staging: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file() or path.as_posix().endswith("data/build_manifest.json"):
            continue
        entries.append(
            {
                "path": path.relative_to(staging).as_posix(),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return entries


def _validate_staging(staging: Path, manifest: Mapping[str, Any]) -> None:
    expected = {str(item["path"]): str(item["sha256"]) for item in manifest.get("generated_files", [])}
    actual = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file() and path.relative_to(staging).as_posix() != "data/build_manifest.json"
    }
    if set(expected) != actual:
        raise ValueError(f"staging tree differs from manifest: missing={set(expected) - actual}, extra={actual - set(expected)}")
    for relative, digest in expected.items():
        if sha256_file(staging / relative) != digest:
            raise ValueError(f"staging file hash mismatch: {relative}")
    build_id = str(manifest.get("build_id") or "")
    html_paths = sorted(staging.rglob("*.html"))
    if not html_paths:
        raise ValueError("staging site contains no HTML")
    for path in html_paths:
        text = _strict_text(path)
        if f'<meta name="build-id" content="{build_id}">' not in text or f'data-build-id="{build_id}"' not in text:
            raise ValueError(f"page build_id mismatch: {path.relative_to(staging)}")
        for href in re.findall(r'href="([^"]+)"', text, flags=re.I):
            if re.match(r"(?i)^(javascript|data|file):", href):
                raise ValueError(f"unsafe URL scheme in {path.relative_to(staging)}: {href}")
            if re.match(r"(?i)^(https|mailto):", href) or href.startswith("#"):
                continue
            target = (path.parent / href.split("#", 1)[0].split("?", 1)[0]).resolve()
            if not target.exists() or staging.resolve() not in (target, *target.parents):
                raise ValueError(f"broken internal link in {path.relative_to(staging)}: {href}")
        lowered = text.lower()
        for token in ("<script", "<iframe", "<object", "<embed", " onerror=", " onclick=", "javascript:"):
            if token in lowered:
                raise ValueError(f"unsafe HTML in {path.relative_to(staging)}: {token}")


def _promote_validated_staging(staging: Path, output: Path) -> None:
    """Promote a validated tree while preserving the previous tree on failure.

    This is a transactional staging promotion, not a claim that every Windows
    filesystem provides a single directory-level atomic swap.
    """

    backup = output.with_name(f".{output.name}-backup-{uuid.uuid4().hex[:8]}")
    promoted = False
    if output.exists():
        output.replace(backup)
    try:
        staging.replace(output)
        promoted = True
    except Exception:
        if backup.exists() and not output.exists():
            backup.replace(output)
        raise
    finally:
        if promoted and backup.exists():
            shutil.rmtree(backup)


def build_site(
    *,
    root: Path,
    output_dir: Path | None = None,
    generated_at: datetime | None = None,
) -> list[Path]:
    root = root.resolve()
    reports_dir = root / "reports"
    site_data = root / "site_data"
    output = (output_dir or root / "site").resolve()
    now = generated_at or datetime.now(SHANGHAI_TZ)
    if now.tzinfo is None:
        raise ValueError("site build generated_at must be timezone-aware")
    now = now.astimezone(SHANGHAI_TZ)
    report_path, report = _latest_structured_report(reports_dir)
    snapshot_path = site_data / "holdings_snapshot.json"
    advice_path = site_data / "advice_accuracy.json"
    advice_history_path = site_data / "advice_history.jsonl"
    advice_history_manifest_path = site_data / "advice_history_manifest.json"
    steady_path = site_data / "steady_income.json"
    for mandatory in (
        snapshot_path,
        advice_path,
        advice_history_path,
        advice_history_manifest_path,
        steady_path,
    ):
        if not mandatory.exists():
            raise FileNotFoundError(f"mandatory site input missing: {mandatory}")
    snapshot = read_json_strict(snapshot_path)
    advice = read_json_strict(advice_path)
    steady = read_json_strict(steady_path)
    history = read_jsonl_strict(advice_history_path)
    history_manifest = read_json_strict(advice_history_manifest_path)
    if not isinstance(snapshot, dict) or not isinstance(advice, dict) or not isinstance(steady, dict):
        raise ValueError("site inputs must be JSON objects")
    if not isinstance(history_manifest, dict):
        raise ValueError("advice history manifest must be an object")
    if int(history_manifest.get("count", -1)) != len(history):
        raise ValueError("advice history manifest count mismatch")
    if str(history_manifest.get("sha256") or "") != sha256_file(advice_history_path):
        raise ValueError("advice history manifest hash mismatch")
    _validate_holdings(snapshot)
    _validate_steady(steady)
    if advice.get("evaluation_version") != ADVICE_EVALUATION_VERSION:
        raise ValueError("advice evaluation_version does not match current evaluator")
    _validate_input_coherence(report, snapshot, advice, steady)

    git_sha = _git_sha(root)
    git_dirty = _git_dirty(root)
    source_hash = source_fingerprint(root)
    input_hashes = {
        "report": sha256_file(report_path),
        "snapshot": sha256_file(snapshot_path),
        "advice": sha256_file(advice_path),
        "history": sha256_file(advice_history_path),
        "history_manifest": sha256_file(advice_history_manifest_path),
        "steady": sha256_file(steady_path),
    }
    identity_payload = {
        "source_fingerprint": source_hash,
        "site_renderer_version": SITE_RENDERER_VERSION,
        "input_hashes": input_hashes,
        "versions": {
            "build_manifest_schema": BUILD_MANIFEST_SCHEMA_VERSION,
            "report_schema": report.get("schema_version"),
            "advice_evaluator": advice.get("evaluation_version"),
            "steady_model": steady.get("model_version"),
            "steady_evaluator": steady.get("evaluator_version"),
            "steady_evidence": steady.get("evidence_version"),
            "steady_ruleset": steady.get("ruleset_version"),
            "steady_sector_model": steady.get("sector_model_version"),
            "steady_price_model": steady.get("price_model_version"),
        },
    }
    content_id = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    build_id = f"content-{content_id[:20]}"
    rendered_at = _content_timestamp(report)

    raw_path = reports_dir / f"report_{_date_key(report['report_date'])}.md"
    raw_markdown = _strict_text(raw_path) if raw_path.exists() else ""
    staging = output.with_name(f".{output.name}-staging-{uuid.uuid4().hex[:8]}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        _write_bytes(staging / ".nojekyll", b"")
        _write_bytes(staging / "assets" / "site.css", SITE_CSS.encode("utf-8"))
        _write_bytes(
            staging / "reports" / f"report_{_date_key(report['report_date'])}.html",
            _render_stock_report(report, snapshot, raw_markdown, build_id=build_id).encode("utf-8"),
        )
        market_pages: list[tuple[str, str]] = []
        for path in reports_dir.glob("market_review_*.md"):
            match = MARKET_MD_RE.fullmatch(path.name)
            if not match:
                continue
            day = f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}"
            href = f"reports/market_review_{match.group(1)}.html"
            market_pages.append((day, href))
            _write_bytes(staging / href, _render_market_page(path, build_id=build_id).encode("utf-8"))
        accounts = snapshot.get("accounts") if isinstance(snapshot.get("accounts"), Mapping) else {}
        for account, groups in accounts.items():
            if isinstance(groups, Mapping):
                _write_bytes(
                    staging / "accounts" / _account_slug(str(account)),
                    _render_account_page(str(account), groups, report, build_id=build_id).encode("utf-8"),
                )
        public_advice = _public_accuracy(advice)
        _write_bytes(staging / "advice_backtest.html", _render_advice(public_advice, build_id=build_id).encode("utf-8"))
        _write_bytes(staging / "steady_income.html", _render_steady(steady, build_id=build_id).encode("utf-8"))
        _write_bytes(staging / "reports" / "index.html", _render_archive(str(report["report_date"]), market_pages, build_id=build_id).encode("utf-8"))
        _write_bytes(
            staging / "index.html",
            _render_index(
                report,
                snapshot,
                market_pages=market_pages,
                build_id=build_id,
                generated_at=rendered_at,
            ).encode("utf-8"),
        )

        public_history = [public_advice_record(record) for record in history]
        _write_bytes(staging / "data" / "holdings_snapshot.json", _json_bytes(snapshot))
        _write_bytes(staging / "data" / "advice_accuracy.json", _json_bytes(public_advice))
        _write_bytes(
            staging / "data" / "advice_history.jsonl",
            b"".join((json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8") for item in public_history),
        )
        _write_bytes(staging / "data" / "advice_history_manifest.json", _json_bytes(history_manifest))
        _write_bytes(staging / "data" / "steady_income.json", _json_bytes(steady))
        _write_bytes(staging / "data" / report_path.name, _json_bytes(report))

        manifest = {
            "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
            "build_id": build_id,
            "content_id": content_id,
            "git_sha": git_sha,
            "git_dirty": git_dirty,
            "source_fingerprint": source_hash,
            "site_renderer_version": SITE_RENDERER_VERSION,
            "built_at": now.isoformat(timespec="seconds"),
            "report_id": report.get("run_id"),
            "report_date": report.get("report_date"),
            "report_schema_version": report.get("schema_version"),
            "report_sha256": sha256_file(report_path),
            "holdings_snapshot_sha256": sha256_file(snapshot_path),
            "holdings_snapshot_generated_at": snapshot.get("generated_at"),
            "advice_history_sha256": sha256_file(advice_history_path),
            "advice_history_count": len(history),
            "advice_evaluation_version": advice.get("evaluation_version"),
            "steady_income_sha256": sha256_file(steady_path),
            "steady_income_as_of": steady.get("as_of"),
            "steady_income_model_version": steady.get("model_version"),
            "steady_income_evaluator_version": steady.get("evaluator_version"),
            "steady_income_ruleset_version": steady.get("ruleset_version"),
            "steady_income_sector_model_version": steady.get("sector_model_version"),
            "steady_income_evidence_version": steady.get("evidence_version"),
            "steady_income_price_model_version": steady.get("price_model_version"),
            "manifest_file": "data/build_manifest.json",
            "manifest_in_generated_files": False,
            "generated_files": _manifest_file_entries(staging),
        }
        _write_bytes(staging / "data" / "build_manifest.json", _json_bytes(manifest))
        _validate_staging(staging, manifest)
        # Run the same complete contract used by the deployment gate before
        # replacing the last known-good public tree.
        from src.site.validator import validate_site

        contract_errors = validate_site(staging)
        if contract_errors:
            raise ValueError("staging site contract failed:\n- " + "\n- ".join(contract_errors))
        _promote_validated_staging(staging, output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return [path for path in output.rglob("*") if path.is_file()]
