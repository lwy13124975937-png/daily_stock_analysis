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
    "GeminiException",
    "ServiceUnavailableError",
    "RESOURCE_EXHAUSTED",
    "ResourceExhausted",
    "quota exceeded",
    "litellm.ServiceUnavailableError",
    '"error":',
    "traceback",
    '"code"',
    '"message"',
)
SENSITIVE_TOKENS = (
    "unit_cost",
    "shares",
    "cost",
    "market_value",
    "profit",
    "amount",
    "total",
    "成本",
    "份额",
    "金额",
    "账户金额",
    "总资产",
    "持仓成本",
    "单位成本",
    "市值",
    "盈亏",
)
FUND_DECISION_TOKENS = ("买入", "卖出", "观望", "评分", "评级", "打分", "交易评级", "股票评级", "交易建议")
BAD_LOF_TEXT_TOKENS = (
    "不输出逐个标的观察或配置观察",
)
LOF_SINGLE_NOTE = "已纳入账户级 LOF/ETF 组合复盘"
TRUNCATED_SUFFIXES = (
    "基于当前持仓清单做",
    "呈现出明显的",
    "当前处于典型的",
    "典型的",
)
NATURAL_ENDINGS = tuple("。；;：:、，,）)】》”’！？?!…")


def html_pages() -> list[Path]:
    pages: list[Path] = []
    index = SITE_DIR / "index.html"
    if index.exists():
        pages.append(index)
    if SITE_REPORTS_DIR.exists():
        pages.extend(sorted(SITE_REPORTS_DIR.glob("*.html")))
    if SITE_ACCOUNTS_DIR.exists():
        pages.extend(sorted(SITE_ACCOUNTS_DIR.glob("*.html")))
    return pages


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
            r"<section class=\"panel\">\s*<h3>[^<]*LOF/ETF 组合复盘</h3>(.*?)</section>",
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
            r"<section class=\"panel\">\s*<h3>[^<]*场外基金组合复盘</h3>(.*?)</section>",
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


def main() -> int:
    pages = html_pages()
    if not pages:
        print(f"ERROR: no generated HTML pages under {SITE_DIR}")
        return 1

    errors: list[str] = []

    for page in pages:
        html = page.read_text(encoding="utf-8", errors="ignore")
        account_html = strip_raw_report(html)

        _check_tokens(errors, page, "public page", html, SENSITIVE_TOKENS, "contains sensitive token")
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
