#!/usr/bin/env python3
"""Validate generated holding report HTML presentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SITE_REPORTS_DIR = ROOT_DIR / "site" / "reports"
RAW_REPORT_MARKER = "<summary>原始 AI 股票日报</summary>"
BAD_SUMMARY_TOKENS = ("**", "###", "####", "AI摘要缺失")
BAD_ACCOUNT_MARKDOWN_TOKENS = ("**", "###", "####", "```", "AI摘要缺失")
BAD_ACCOUNT_ERROR_TOKENS = ("ResourceExhausted", "quota exceeded", '"error"', '"code"', '"message"')
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
)
FUND_DECISION_TOKENS = ("买入", "卖出", "观望", "评分")


def latest_report_html() -> Path | None:
    if not SITE_REPORTS_DIR.exists():
        return None
    reports = list(SITE_REPORTS_DIR.glob("report_*.html"))
    if not reports:
        return None
    return max(reports, key=lambda path: path.stat().st_mtime)


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


def main() -> int:
    report_path = latest_report_html()
    if report_path is None:
        print(f"ERROR: no generated report HTML under {SITE_REPORTS_DIR}")
        return 1

    html = report_path.read_text(encoding="utf-8", errors="ignore")
    account_html = strip_raw_report(html)
    errors: list[str] = []

    summary_blocks = extract_account_summary_blocks(account_html)
    if not summary_blocks:
        errors.append("no account summary blocks found")
    for idx, block in enumerate(summary_blocks, start=1):
        for token in BAD_SUMMARY_TOKENS:
            if token in block:
                errors.append(f"account summary block {idx} contains {token!r}")

    for token in BAD_ACCOUNT_MARKDOWN_TOKENS:
        if token in account_html:
            errors.append(f"account holding area contains raw Markdown token {token!r}")

    for token in BAD_ACCOUNT_ERROR_TOKENS:
        if token in account_html:
            errors.append(f"account holding area contains raw error token {token!r}")

    for token in SENSITIVE_TOKENS:
        if token in account_html:
            errors.append(f"account holding area contains sensitive token {token!r}")

    for idx, block in enumerate(extract_lof_blocks(account_html), start=1):
        for token in FUND_DECISION_TOKENS:
            if token in block:
                errors.append(f"LOF/ETF block {idx} contains stock decision token {token!r}")

    print(f"checked report html: {report_path.relative_to(ROOT_DIR)}")
    if errors:
        print("ERROR: report HTML presentation check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("report HTML presentation check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
