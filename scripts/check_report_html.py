#!/usr/bin/env python3
"""Validate generated holding report HTML presentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from html import unescape


ROOT_DIR = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT_DIR / "site"
SITE_REPORTS_DIR = ROOT_DIR / "site" / "reports"
SITE_ACCOUNTS_DIR = ROOT_DIR / "site" / "accounts"
RAW_REPORT_MARKER = "<summary>原始 AI 股票日报</summary>"
REPORT_HTML_RE = re.compile(r"report_(20\d{6})\.html$")
BAD_SUMMARY_TOKENS = ("**", "#", "###", "####", "```", "|---------|", "AI摘要缺失")
BAD_ACCOUNT_MARKDOWN_TOKENS = (
    "**",
    "### #",
    "#### #",
    "## #",
    "```",
    "# 重要信息速览",
    "# 当日行情",
    "# 数据透视",
    "|---------|",
    "| 持仓情况 |",
    "AI摘要缺失",
    "报告生成时间",
    "分析模型",
    "report generated time",
    "gemini/gemini",
)
BAD_ACCOUNT_ERROR_TOKENS = (
    "All LLM models failed",
    "Exception",
    "GeminiException",
    "ServiceUnavailableError",
    "RESOURCE_EXHAUSTED",
    "ResourceExhausted",
    "quota exceeded",
    "litellm.ServiceUnavailableError",
    '"error":',
    "traceback",
    "Traceback",
    "模型输出疑似截断",
    "LLM 未返回内容",
    "本次组合复盘未完成",
    '"code"',
    '"message"',
)
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
SENSITIVE_FIELD_NAMES = (
    "unit_cost",
    "shares",
    "cost",
    "market_value",
    "profit",
    "amount",
    "total",
)
SENSITIVE_FIELD_RE = re.compile(
    r"(?is)"
    r"(<(?:th|td)\b[^>]*>\s*(?:"
    + "|".join(re.escape(field) for field in SENSITIVE_FIELD_NAMES)
    + r")\s*</(?:th|td)>)"
    r"|([\"'](?:"
    + "|".join(re.escape(field) for field in SENSITIVE_FIELD_NAMES)
    + r")[\"']\s*:)"
    r"|(\b(?:unit_cost|market_value)\b)"
    r"|(\b(?:shares|cost|profit|amount|total)\b\s*[:=,])"
)
FUND_DECISION_TOKENS = ("买入", "卖出", "观望", "评分", "评级", "打分", "交易评级", "股票评级", "交易建议")
BAD_LOF_TEXT_TOKENS = (
    "不输出逐个标的观察或配置观察",
)
LOF_SINGLE_NOTE = "已纳入账户级 LOF/ETF 组合复盘"
MISSING_SNAPSHOT_TEXT = "暂无持仓快照"
TRUNCATED_SUFFIXES = (
    "组合在",
    "基于当前持仓清单做",
    "呈现出明显的",
    "该组合呈现",
    "当前组合在",
    "当前处于典型的",
    "典型的",
)
INCOMPLETE_TAIL_TOKENS = (
    "在",
    "的",
    "和",
    "与",
    "及",
    "但",
    "因此",
    "同时",
    "主要",
    "整体",
    "风格暴露",
)
NATURAL_ENDINGS = tuple("。；;：:、，,）)】》”’！？?!…")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def html_pages() -> list[Path]:
    pages: list[Path] = []
    index = SITE_DIR / "index.html"
    if index.exists():
        pages.append(index)
    if SITE_DIR.exists():
        pages.extend(
            page
            for page in sorted(SITE_DIR.glob("*.html"))
            if page.name != "index.html"
        )
    if SITE_REPORTS_DIR.exists():
        pages.extend(sorted(SITE_REPORTS_DIR.glob("*.html")))
    if SITE_ACCOUNTS_DIR.exists():
        pages.extend(sorted(SITE_ACCOUNTS_DIR.glob("*.html")))
    return pages


def latest_stock_report_html() -> tuple[str, Path] | None:
    if not SITE_REPORTS_DIR.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for page in SITE_REPORTS_DIR.glob("report_20*.html"):
        match = REPORT_HTML_RE.match(page.name)
        if match:
            candidates.append((match.group(1), page))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])


def _date_hyphen(date_key: str) -> str:
    return f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"


def strip_raw_report(html: str) -> str:
    marker_index = html.find(RAW_REPORT_MARKER)
    if marker_index == -1:
        return html
    return html[:marker_index]


def extract_account_summary_blocks(html: str) -> list[str]:
    return re.findall(
        r"<section class=\"panel\">\s*<h3>[^<]*分析结果摘要</h3>(.*?)</section>",
        html,
        flags=re.DOTALL,
    )


def extract_lof_blocks(html: str) -> list[str]:
    blocks = []
    blocks.extend(
        re.findall(
            r'<section\b[^>]*class="[^"]*panel[^"]*"[^>]*>\s*<h3>[^<]*LOF/ETF 组合复盘</h3>(.*?)</section>',
            html,
            flags=re.DOTALL,
        )
    )
    blocks.extend(
        re.findall(
            r"<article class=\"holding-item\"[^>]*data-type=\"lof\"[^>]*>(.*?)</article>",
            html,
            flags=re.DOTALL,
        )
    )
    blocks.extend(
        re.findall(
            r"<li class=\"summary-item\"[^>]*data-type=\"lof\"[^>]*>(.*?)</li>",
            html,
            flags=re.DOTALL,
        )
    )
    return blocks


def extract_otc_blocks(html: str) -> list[str]:
    blocks = []
    blocks.extend(
        re.findall(
            r'<section\b[^>]*class="[^"]*panel[^"]*"[^>]*>\s*<h3>[^<]*场外基金组合复盘</h3>(.*?)</section>',
            html,
            flags=re.DOTALL,
        )
    )
    blocks.extend(
        re.findall(
            r"<article class=\"holding-item\"[^>]*data-type=\"otc\"[^>]*>(.*?)</article>",
            html,
            flags=re.DOTALL,
        )
    )
    blocks.extend(
        re.findall(
            r"<li class=\"summary-item\"[^>]*data-type=\"otc\"[^>]*>(.*?)</li>",
            html,
            flags=re.DOTALL,
        )
    )
    return blocks


def _snippet(text: str, token: str) -> str:
    index = text.find(token)
    if index == -1:
        return token
    start = max(0, index - 40)
    end = min(len(text), index + len(token) + 80)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return unescape(re.sub(r"\s+", " ", text)).strip()


def _current_holding_advice_count(html: str) -> int | None:
    match = re.search(
        r"<h3>\s*当前持仓建议\s*</h3>.*?已记录建议数量：\s*<strong>(\d+)</strong>",
        html,
        flags=re.DOTALL,
    )
    if not match:
        return None
    return int(match.group(1))


def _report_has_stock_holdings(html: str) -> bool:
    account_html = strip_raw_report(html)
    return 'data-type="stock"' in account_html or "A股个股" in account_html


def _check_half_sentence_leaks(errors: list[str], path: Path, scope: str, html: str) -> None:
    text = _strip_tags(html)
    for phrase in TRUNCATED_SUFFIXES:
        pattern = re.compile(re.escape(phrase) + r"(?=\s|$|[。；;：:，,、])")
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 80)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            errors.append(f"{_page_label(path)} {scope} contains truncated phrase {phrase!r}: {snippet}")


def _review_text_units(block: str) -> list[str]:
    units = []
    for tag in ("p", "li"):
        units.extend(
            _strip_tags(match)
            for match in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, flags=re.DOTALL)
        )
    return [unit for unit in units if unit]


def _check_suspicious_truncation(
    errors: list[str],
    path: Path,
    scope: str,
    block: str,
) -> None:
    for unit in _review_text_units(block):
        if any(unit.endswith(suffix) for suffix in TRUNCATED_SUFFIXES):
            errors.append(f"{_page_label(path)} {scope} appears truncated: {unit}")
            continue
        if any(unit.endswith(token) for token in INCOMPLETE_TAIL_TOKENS):
            errors.append(f"{_page_label(path)} {scope} has incomplete sentence tail: {unit}")
            continue
        if "AI 组合复盘未完成" in unit and "规则版组合兜底复盘" not in unit:
            errors.append(f"{_page_label(path)} {scope} has incomplete AI fallback notice: {unit}")
            continue
        if unit.count("“") > unit.count("”"):
            errors.append(f"{_page_label(path)} {scope} has unclosed Chinese quote: {unit}")
            continue
        if len(unit) < 40:
            continue
        if not unit.endswith(NATURAL_ENDINGS):
            errors.append(f"{_page_label(path)} {scope} lacks natural sentence ending: {unit}")


def _page_label(path: Path) -> str:
    try:
        return path.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return str(path)


def _check_tokens(
    errors: list[str],
    path: Path,
    scope: str,
    html: str,
    tokens: tuple[str, ...],
    message: str,
) -> None:
    for token in tokens:
        if token in html:
            errors.append(
                f"{_page_label(path)} {scope} {message} {token!r}: {_snippet(html, token)}"
            )


def _check_sensitive_content(errors: list[str], path: Path, scope: str, html: str) -> None:
    for phrase in SENSITIVE_PHRASES:
        if phrase in html:
            errors.append(
                f"{_page_label(path)} {scope} contains sensitive phrase {phrase!r}: "
                f"{_snippet(html, phrase)}"
            )

    for match in SENSITIVE_FIELD_RE.finditer(html):
        token = match.group(0)
        errors.append(
            f"{_page_label(path)} {scope} contains sensitive field {token!r}: "
            f"{_snippet(html, token)}"
        )


def main() -> int:
    pages = html_pages()
    if not pages:
        print(f"ERROR: no generated HTML pages under {SITE_DIR}")
        return 1

    errors: list[str] = []
    latest_report = latest_stock_report_html()
    if latest_report is not None:
        latest_date, latest_page = latest_report
        latest_href = f"reports/{latest_page.name}"
        index_path = SITE_DIR / "index.html"
        if index_path.exists():
            index_html = index_path.read_text(encoding="utf-8", errors="ignore")
            if latest_href not in index_html:
                errors.append(
                    f"site/index.html latest report link is stale: expected {latest_href}"
                )
            if MISSING_SNAPSHOT_TEXT in index_html:
                errors.append("site/index.html shows missing holdings snapshot")
        else:
            errors.append("site/index.html is missing while report pages exist")
        advice_path = SITE_DIR / "advice_backtest.html"
        if advice_path.exists():
            advice_html = advice_path.read_text(encoding="utf-8", errors="ignore")
            latest_text = _date_hyphen(latest_date)
            if latest_text not in advice_html:
                errors.append(
                    f"site/advice_backtest.html is stale: expected latest report date {latest_text}"
                )
            latest_html = latest_page.read_text(encoding="utf-8", errors="ignore")
            current_count = _current_holding_advice_count(advice_html)
            if _report_has_stock_holdings(latest_html) and current_count == 0:
                errors.append(
                    "site/advice_backtest.html has zero current holding advice while latest report contains stock holdings"
                )
        else:
            errors.append("site/advice_backtest.html is missing while report pages exist")

    for page in pages:
        html = page.read_text(encoding="utf-8", errors="ignore")
        account_html = strip_raw_report(html)

        _check_sensitive_content(errors, page, "public page", html)
        _check_tokens(
            errors,
            page,
            "account display area",
            account_html,
            BAD_ACCOUNT_MARKDOWN_TOKENS,
            "contains raw Markdown token",
        )
        _check_tokens(
            errors,
            page,
            "account display area",
            account_html,
            BAD_ACCOUNT_ERROR_TOKENS,
            "contains raw error token",
        )
        if MISSING_SNAPSHOT_TEXT in account_html:
            errors.append(f"{_page_label(page)} account display area shows missing holdings snapshot")
        _check_half_sentence_leaks(errors, page, "account display area", account_html)

        summary_blocks = extract_account_summary_blocks(account_html)
        for idx, block in enumerate(summary_blocks, start=1):
            _check_tokens(
                errors,
                page,
                f"account summary block {idx}",
                block,
                BAD_SUMMARY_TOKENS,
                "contains forbidden token",
            )

        for idx, block in enumerate(extract_lof_blocks(account_html), start=1):
            _check_tokens(
                errors,
                page,
                f"LOF/ETF block {idx}",
                block,
                FUND_DECISION_TOKENS,
                "contains stock decision token",
            )
            _check_tokens(
                errors,
                page,
                f"LOF/ETF block {idx}",
                block,
                BAD_LOF_TEXT_TOKENS,
                "contains invalid LOF/ETF sentence",
            )
            if block.count(LOF_SINGLE_NOTE) > 1:
                errors.append(
                    f"{_page_label(page)} LOF/ETF block {idx} repeats {LOF_SINGLE_NOTE!r}"
                )
            _check_suspicious_truncation(errors, page, f"LOF/ETF block {idx}", block)

        for idx, block in enumerate(extract_otc_blocks(account_html), start=1):
            _check_tokens(
                errors,
                page,
                f"OTC block {idx}",
                block,
                FUND_DECISION_TOKENS,
                "contains stock decision token",
            )
            _check_suspicious_truncation(errors, page, f"OTC block {idx}", block)

    print("checked report html pages:")
    for page in pages:
        print(f"  - {_page_label(page)}")
    if errors:
        print("ERROR: report HTML presentation check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("report HTML presentation check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
