"""Build static HTML pages for reports and account holdings.

The report Markdown files remain the source of analysis content. The optional
``site_data/holdings_snapshot.json`` file supplies public account grouping
metadata generated from stock-dashboard holdings.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT_DIR / "reports"
SITE_DATA_DIR = ROOT_DIR / "site_data"
HOLDINGS_SNAPSHOT_PATH = SITE_DATA_DIR / "holdings_snapshot.json"
STEADY_INCOME_DATA_PATH = SITE_DATA_DIR / "steady_income.json"
SITE_DIR = ROOT_DIR / "site"
SITE_REPORTS_DIR = SITE_DIR / "reports"
SITE_ACCOUNTS_DIR = SITE_DIR / "accounts"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

DISCLAIMER = "本页面内容由 AI 自动生成，仅作复盘参考，不构成投资建议。"
SOURCE_TEXT = "stock-dashboard 最新 holdings_data.json"
ACCOUNT_SLUGS = {
    "东方财富": "eastmoney",
    "银河证券": "galaxy",
    "支付宝": "alipay",
}
ACCOUNT_ORDER = ("东方财富", "银河证券", "支付宝")
TYPE_LABELS = {
    "stock": "A股个股",
    "lof": "场内基金/ETF/LOF",
    "otc": "场外基金",
}
TYPE_ORDER = ("stock", "lof", "otc")
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
GLOBAL_SECTION_KEYWORDS = (
    "摘要",
    "总结",
    "汇总",
    "总览",
    "整体",
    "概览",
    "榜单",
)
MAX_AI_SNIPPETS_PER_CODE = 3
MAX_AI_SNIPPET_CHARS = 500
TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
SKIP_DETAIL_HEADING_KEYWORDS = (
    "当日行情",
    "数据透视",
    "检查清单",
    "持仓情况",
    "操作建议",
    "价格指标",
)
SKIP_DETAIL_LINE_KEYWORDS = (
    "报告生成时间",
    "分析模型",
    "report generated time",
    "generated time",
    "analysis model",
    "model:",
    "model：",
    "gemini/",
)
FUND_DECISION_WORD_REPLACEMENTS = (
    ("买入", "配置观察"),
    ("卖出", "风险观察"),
    ("观望", "继续跟踪"),
    ("交易建议", "配置观察"),
    ("交易评级", "配置观察"),
    ("股票评级", "配置观察"),
    ("评级", "观察结论"),
    ("打分", "观察"),
    ("评分", "观察"),
    ("逐只股票", "逐个标的"),
    ("个股技术面", "标的短线波动"),
)
RAW_ERROR_TOKENS = (
    "All LLM models failed",
    "GeminiException",
    "ServiceUnavailable",
    "ServiceUnavailableError",
    "RESOURCE_EXHAUSTED",
    "ResourceExhausted",
    "litellm.",
    '"error":',
)


@dataclass(frozen=True)
class ReportPage:
    source: Path
    output: Path
    title: str
    kind: str
    sort_key: tuple[str, float, str]


@dataclass(frozen=True)
class AccountPage:
    account: str
    output: Path
    counts: dict[str, int]


@dataclass(frozen=True)
class MarkdownSection:
    heading: str
    body: str
    level: int = 0


@dataclass(frozen=True)
class MarkdownHeading:
    line_index: int
    level: int
    text: str


@dataclass(frozen=True)
class HoldingReportContext:
    summary_by_code: dict[str, list[str]]
    snippets_by_code: dict[str, list[str]]
    unfinished_by_code: dict[str, list[str]]
    lof_reviews_by_account: dict[str, str]
    otc_reviews_by_account: dict[str, str]
    unmatched: dict[str, list[str]]


def _load_markdown_renderer():
    """Prefer the project's existing renderer, then fall back to markdown2."""
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

    try:
        from src.formatters import markdown_to_html_document

        return markdown_to_html_document
    except Exception as exc:
        try:
            import markdown2
        except Exception:
            raise RuntimeError(
                "Cannot import src.formatters.markdown_to_html_document or markdown2"
            ) from exc

        def render(markdown_text: str) -> str:
            body = markdown2.markdown(
                markdown_text,
                extras=["tables", "fenced-code-blocks", "break-on-newline", "cuddled-lists"],
            )
            return _wrap_html("Report", body)

        return render


def _extract_title(markdown_text: str, fallback: str) -> str:
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return fallback


def _format_date(date_key: str) -> str:
    if re.fullmatch(r"20\d{6}", date_key):
        return f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
    return date_key


def _extract_date_key(path: Path) -> str:
    match = re.search(r"(20\d{6})", path.stem)
    if match:
        return match.group(1)
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")


def _report_kind(path: Path) -> str:
    if re.fullmatch(r"market_review_20\d{6}", path.stem):
        return "market"
    if re.fullmatch(r"report_20\d{6}", path.stem):
        return "stock"
    return "other"


def _friendly_report_title(path: Path, markdown_text: str) -> str:
    date_text = _format_date(_extract_date_key(path))
    kind = _report_kind(path)
    if kind == "market":
        return f"{date_text} 大盘复盘"
    if kind == "stock":
        return f"{date_text} 持仓日报"
    return _extract_title(markdown_text, path.stem)


def _html_name(path: Path) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip(".-")
    return f"{safe_stem or 'report'}.html"


def _account_slug(account: str) -> str:
    if account in ACCOUNT_SLUGS:
        return ACCOUNT_SLUGS[account]
    ascii_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", account).strip(".-").lower()
    return ascii_slug or quote(account, safe="")


def _relative_href(path: Path) -> str:
    return path.relative_to(SITE_DIR).as_posix()


def _now_text() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _load_holdings_snapshot() -> dict:
    if not HOLDINGS_SNAPSHOT_PATH.exists():
        print(f"No holdings snapshot found: {HOLDINGS_SNAPSHOT_PATH}")
        return {}
    try:
        return json.loads(HOLDINGS_SNAPSHOT_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"Failed to read holdings snapshot: {type(exc).__name__}: {exc}")
        return {}


def _load_steady_income_data() -> dict:
    if not STEADY_INCOME_DATA_PATH.exists():
        print(f"No steady-income dataset found: {STEADY_INCOME_DATA_PATH}")
        return {}
    try:
        payload = json.loads(STEADY_INCOME_DATA_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"Failed to read steady-income dataset: {type(exc).__name__}: {exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _counts_for_account(groups: dict) -> dict[str, int]:
    return {asset_type: len(groups.get(asset_type, []) or []) for asset_type in TYPE_ORDER}


def _account_items(groups: dict, allowed_types: set[str] | None = None) -> list[dict]:
    if not isinstance(groups, dict):
        return []
    items: list[dict] = []
    ordered_types = list(TYPE_ORDER)
    for asset_type in groups:
        if asset_type not in ordered_types:
            ordered_types.append(asset_type)
    for asset_type in ordered_types:
        if allowed_types is not None and asset_type not in allowed_types:
            continue
        type_items = groups.get(asset_type, []) or []
        if isinstance(type_items, list):
            items.extend(item for item in type_items if isinstance(item, dict))
    return items


def _display_holding_name(value: object, code: str) -> str:
    name = str(value or "").strip() or "-"
    if not code:
        return name
    return re.sub(
        rf"\s*[（(]\s*{re.escape(code)}\s*[）)]\s*$",
        "",
        name,
    ).strip() or name


def _ordered_account_names(accounts: dict) -> list[str]:
    if not isinstance(accounts, dict):
        return []
    ordered = [account for account in ACCOUNT_ORDER if account in accounts]
    ordered.extend(account for account in accounts if account not in ordered)
    return ordered


def _latest_report(pages: list[ReportPage], kind: str) -> ReportPage | None:
    return next((page for page in pages if page.kind == kind), None)


def _all_snapshot_codes(snapshot: dict) -> set[str]:
    codes: set[str] = set()
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(accounts, dict):
        return codes
    for groups in accounts.values():
        if not isinstance(groups, dict):
            continue
        for items in groups.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    code = str(item.get("code", "")).strip()
                    if code:
                        codes.add(code)
    return codes


def _codes_in_text(text: str) -> list[str]:
    return list(dict.fromkeys(CODE_RE.findall(text)))


def _heading_match(line: str) -> tuple[int, str] | None:
    match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
    if not match:
        return None
    return len(match.group(1)), match.group(2).strip()


def _heading_text(line: str) -> str | None:
    heading = _heading_match(line)
    if heading is None:
        return None
    return heading[1]


def _is_global_ai_heading(text: str) -> bool:
    if _codes_in_text(text):
        return False
    compact = re.sub(r"\s+", "", text)
    return any(keyword in compact for keyword in GLOBAL_SECTION_KEYWORDS)


def _clean_heading_text(text: str) -> str:
    return re.sub(r"[*_`~]+", "", text or "").strip()


def _plain_markdown_text(text: str) -> str:
    clean = text or ""
    clean = re.sub(r"^\s{0,3}#{1,6}\s*", "", clean)
    clean = re.sub(r"^[-*+]\s+", "", clean.strip())
    clean = re.sub(r"^\d+[.)、]\s+", "", clean)
    clean = re.sub(r"`{3,}", "", clean)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    clean = re.sub(r"[*_~]+", "", clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def _markdown_headings(markdown_text: str) -> list[MarkdownHeading]:
    headings: list[MarkdownHeading] = []
    for idx, line in enumerate(markdown_text.splitlines()):
        heading = _heading_match(line)
        if heading is None:
            continue
        level, text = heading
        headings.append(MarkdownHeading(idx, level, _clean_heading_text(text)))
    return headings


def _markdown_sections(markdown_text: str) -> list[MarkdownSection]:
    sections: list[MarkdownSection] = []
    current_heading = ""
    current_level = 0
    current_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if current_heading or body:
            sections.append(MarkdownSection(current_heading, body, current_level))

    for line in markdown_text.splitlines():
        heading = _heading_match(line)
        if heading is not None:
            flush()
            current_level, heading_text = heading
            current_heading = _clean_heading_text(heading_text)
            current_lines = []
            continue
        current_lines.append(line)

    flush()
    return sections


def _short_paragraphs(text: str) -> list[str]:
    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text or "") if block.strip()]
    if paragraphs:
        return paragraphs
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _holding_name_code_pairs(holdings: list[dict] | None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in holdings or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "") or "").strip()
        name = _display_holding_name(item.get("name", ""), code)
        if name and code:
            pairs.append((name, code))
    return pairs


def _append_ai_snippet(
    by_code: dict[str, list[str]],
    code: str,
    snippet: str,
    *,
    max_chars: int | None = MAX_AI_SNIPPET_CHARS,
    skip_raw_errors: bool = True,
) -> None:
    text = snippet.strip()
    if not text:
        return
    if skip_raw_errors and _looks_like_raw_error(text):
        return
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    if re.fullmatch(r"[\s|:：\-]+", text):
        return
    existing = by_code.setdefault(code, [])
    if text in existing or len(existing) >= MAX_AI_SNIPPETS_PER_CODE:
        return
    existing.append(text)


def _looks_like_raw_error(text: str) -> bool:
    lower = (text or "").lower()
    return any(token.lower() in lower for token in RAW_ERROR_TOKENS)


def _append_section_snippets(
    by_code: dict[str, list[str]],
    code: str,
    body: str,
    *,
    allow_code_less_paragraphs: bool,
) -> None:
    for paragraph in _short_paragraphs(body):
        codes = _codes_in_text(paragraph)
        if len(codes) > 1:
            continue
        if len(codes) == 1 and codes[0] != code:
            continue
        if len(codes) == 0 and not allow_code_less_paragraphs:
            continue
        _append_ai_snippet(by_code, code, paragraph)


def _holding_code_from_title(text: str, name_code_pairs: list[tuple[str, str]]) -> str | None:
    clean = _clean_heading_text(text)
    codes = _codes_in_text(clean)
    if len(codes) == 1:
        return codes[0]
    if codes:
        return None
    matches = [code for name, code in name_code_pairs if name and name in clean]
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    return None


def _is_non_holding_report_boundary(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_heading_text(text))
    return (
        _is_global_ai_heading(text)
        or "未完成分析标的" in compact
        or "LOF/ETF组合复盘" in compact
        or "场外基金组合复盘" in compact
    )


def _is_next_holding_boundary(text: str, name_code_pairs: list[tuple[str, str]]) -> bool:
    return _holding_code_from_title(text, name_code_pairs) is not None


def _ordered_title_text(line: str) -> str | None:
    match = re.match(r"^\s*\d+[.)、]\s+(.+?)\s*$", line)
    if not match:
        return None
    title = _clean_heading_text(match.group(1))
    # Numbered summary rows usually include a colon and should not become
    # per-holding sections.
    if "：" in title or ":" in title or "|" in title:
        return None
    return title


def _append_full_markdown_section(
    by_code: dict[str, list[str]],
    code: str | None,
    title: str,
    lines: list[str],
    start: int,
    end: int,
    name_code_pairs: list[tuple[str, str]],
) -> None:
    if code is None:
        return
    if _is_global_ai_heading(title):
        return
    section_text = "\n".join(lines[start:end]).strip()
    if not section_text:
        return
    body_text = "\n".join(lines[start + 1:end]).strip()
    if not _codes_in_text(title):
        # Name-only headings must be confirmed by the body/nearby content.
        if code not in _codes_in_text(body_text):
            return
    _append_ai_snippet(by_code, code, section_text, max_chars=None)


def _extract_heading_sections(
    markdown_text: str,
    name_code_pairs: list[tuple[str, str]],
) -> dict[str, list[str]]:
    by_code: dict[str, list[str]] = {}
    lines = markdown_text.splitlines()
    headings = _markdown_headings(markdown_text)

    for pos, heading in enumerate(headings):
        code = _holding_code_from_title(heading.text, name_code_pairs)
        if code is None:
            continue
        end = len(lines)
        for next_heading in headings[pos + 1:]:
            if _is_next_holding_boundary(next_heading.text, name_code_pairs) or _is_non_holding_report_boundary(next_heading.text):
                end = next_heading.line_index
                break
        _append_full_markdown_section(
            by_code,
            code,
            heading.text,
            lines,
            heading.line_index,
            end,
            name_code_pairs,
        )

    ordered_indices: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        title = _ordered_title_text(line)
        if not title:
            continue
        code = _holding_code_from_title(title, name_code_pairs)
        if code:
            ordered_indices.append((idx, title, code))

    heading_starts = [heading.line_index for heading in headings]
    ordered_starts = [idx for idx, _, _ in ordered_indices]
    for pos, (idx, title, code) in enumerate(ordered_indices):
        end = len(lines)
        later_boundaries = [
            boundary
            for boundary in heading_starts + ordered_starts[pos + 1:]
            if boundary > idx
        ]
        if later_boundaries:
            end = min(later_boundaries)
        _append_full_markdown_section(
            by_code,
            code,
            title,
            lines,
            idx,
            end,
            name_code_pairs,
        )

    return by_code


def _extract_ai_snippets(markdown_text: str, holdings: list[dict] | None = None) -> dict[str, list[str]]:
    name_code_pairs = _holding_name_code_pairs(holdings)
    by_code = _extract_heading_sections(markdown_text, name_code_pairs)

    for section in _markdown_sections(markdown_text):
        heading = section.heading
        body = section.body
        compact_heading = re.sub(r"\s+", "", heading)
        if (
            "未完成分析标的" in compact_heading
            or "LOF/ETF组合复盘" in compact_heading
            or "场外基金组合复盘" in compact_heading
        ):
            continue
        heading_codes = _codes_in_text(heading)
        if len(heading_codes) == 1 and heading_codes[0] not in by_code:
            _append_section_snippets(
                by_code,
                heading_codes[0],
                body,
                allow_code_less_paragraphs=True,
            )
            continue
        if heading_codes or _is_global_ai_heading(heading):
            continue

        body_codes = _codes_in_text(body)
        matched = [
            code
            for name, code in name_code_pairs
            if name and name in heading and code in body_codes
        ]
        if len(set(matched)) == 1:
            _append_section_snippets(
                by_code,
                matched[0],
                body,
                allow_code_less_paragraphs=True,
            )
            continue

        for paragraph in _short_paragraphs(body):
            codes = _codes_in_text(paragraph)
            if len(codes) == 1 and codes[0] not in by_code:
                _append_ai_snippet(by_code, codes[0], paragraph)

    return by_code


def _extract_ai_summary_items(markdown_text: str) -> dict[str, list[str]]:
    by_code: dict[str, list[str]] = {}
    for section in _markdown_sections(markdown_text):
        if "分析结果摘要" not in re.sub(r"\s+", "", section.heading):
            continue
        for line in section.body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            item = re.sub(r"^[-*+]\s+", "", stripped)
            item = re.sub(r"^\d+[.)、]\s+", "", item)
            codes = _codes_in_text(item)
            if len(codes) != 1:
                continue
            _append_ai_snippet(by_code, codes[0], item)

    return by_code


def _extract_unfinished_items(markdown_text: str) -> dict[str, list[str]]:
    by_code: dict[str, list[str]] = {}
    for section in _markdown_sections(markdown_text):
        if "未完成分析标的" not in re.sub(r"\s+", "", section.heading):
            continue
        current_lines: list[str] = []

        def flush() -> None:
            if not current_lines:
                return
            item = "\n".join(current_lines).strip()
            first_code = CODE_RE.search(item)
            if first_code is None:
                return
            code = first_code.group(1)
            _append_ai_snippet(
                by_code,
                code,
                item,
                max_chars=MAX_AI_SNIPPET_CHARS,
                skip_raw_errors=False,
            )

        for line in section.body.splitlines():
            if re.match(r"^\s*[-*+]\s+", line):
                flush()
                current_lines = [line]
            elif current_lines:
                current_lines.append(line)
        flush()
    return by_code


def _render_failure_snippets(failures: list[str], code: str) -> str:
    if not failures:
        return ""
    return f'<p class="note">分析失败：{escape(_failure_detail_text(failures[0], code))}</p>'


def _extract_account_portfolio_reviews(markdown_text: str, compact_section_title: str) -> dict[str, str]:
    lines = markdown_text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        heading = _heading_match(line)
        if heading is None:
            continue
        level, text = heading
        compact = re.sub(r"\s+", "", _clean_heading_text(text))
        if level == 2 and compact_section_title in compact:
            start = idx + 1
            break
    if start is None:
        return {}

    end = len(lines)
    for idx in range(start, len(lines)):
        heading = _heading_match(lines[idx])
        if heading is not None and heading[0] <= 2:
            end = idx
            break

    reviews: dict[str, list[str]] = {}
    current_account = ""
    current_lines: list[str] = []

    def flush() -> None:
        if current_account and current_lines:
            reviews[current_account] = current_lines.copy()

    for line in lines[start:end]:
        heading = _heading_match(line)
        if heading is not None and heading[0] == 3:
            flush()
            current_account = _clean_heading_text(heading[1])
            current_lines = []
            continue
        if current_account:
            current_lines.append(line)
    flush()

    return {
        account: "\n".join(body).strip()
        for account, body in reviews.items()
        if "\n".join(body).strip()
    }


def _extract_lof_portfolio_reviews(markdown_text: str) -> dict[str, str]:
    return _extract_account_portfolio_reviews(markdown_text, "LOF/ETF组合复盘")


def _extract_otc_portfolio_reviews(markdown_text: str) -> dict[str, str]:
    return _extract_account_portfolio_reviews(markdown_text, "场外基金组合复盘")


FUND_REVIEW_TRUNCATED_SUFFIXES = (
    "组合在",
    "当前组合在",
    "该组合呈现",
    "呈现出明显的",
    "基于当前持仓清单做",
    "基于当前持仓清单",
    "风格暴露",
)
FUND_REVIEW_INCOMPLETE_TAILS = (
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
)
FUND_REVIEW_NATURAL_ENDINGS = tuple("。；;：:、，,）)】》”’！？?!…")


def _fund_review_looks_truncated(text: str) -> bool:
    plain = _plain_markdown_text(text)
    units = [unit.strip() for unit in re.split(r"[\n\r]+", plain) if unit.strip()]
    if not units:
        return True
    for unit in units:
        if any(unit.endswith(suffix) for suffix in FUND_REVIEW_TRUNCATED_SUFFIXES):
            return True
        if len(unit) <= 12 and any(unit.endswith(tail) for tail in FUND_REVIEW_INCOMPLETE_TAILS):
            return True
        if len(unit) >= 24 and not unit.endswith(FUND_REVIEW_NATURAL_ENDINGS):
            return True
        if unit.count("“") > unit.count("”"):
            return True
    return False


def _fund_theme_summary(holdings: list[dict]) -> str:
    theme_keywords = (
        ("科技成长", ("科技", "AI", "人工智能", "半导体", "芯片", "算力", "互联网")),
        ("基建链", ("电网", "基建", "工程", "建筑")),
        ("资源品", ("有色", "黄金", "稀土", "资源", "矿业", "铜", "铝", "白银")),
        ("红利价值", ("红利", "股息", "价值")),
        ("海外资产", ("纳斯达克", "标普", "美国", "全球", "海外", "QDII", "港美")),
        ("医药", ("医药", "生物", "创新药")),
        ("新能源", ("新能源", "光伏", "电池", "电力设备")),
        ("宽基指数", ("沪深300", "中证500", "A500", "宽基")),
        ("固收债券", ("债", "纯债", "固收")),
    )
    matched: list[str] = []
    names = " ".join(str(item.get("name", "")) for item in holdings)
    for label, keywords in theme_keywords:
        if any(keyword.lower() in names.lower() for keyword in keywords):
            matched.append(label)
    return "、".join(dict.fromkeys(matched)) if matched else "主题较分散，暂无法从名称准确归类"


def _rule_based_fund_review(account: str, asset_type: str, holdings: list[dict]) -> str:
    count = len(holdings)
    rows = []
    for item in holdings:
        code = str(item.get("code") or "")
        rows.append(f"- {_display_holding_name(item.get('name'), code)}（{code}）")
    themes = _fund_theme_summary(holdings)
    if asset_type == "lof":
        return "\n".join(
            [
                "#### 持有标的",
                *rows,
                "",
                "#### 组合观察",
                "- AI 组合复盘未完成，以下为规则版组合兜底复盘。",
                f"- 该账户持有 {count} 只 LOF/ETF，主要用于场内基金配置观察。",
                f"- 根据名称粗略识别主题：{themes}。",
                "",
                "#### 配置节奏",
                "- 当前仅做组合层面观察，不做单只基金短线判断。",
                "- 后续应重点看对应主题是否延续，以及组合是否过度集中。",
                "",
                "#### 后续观察",
                "- 观察组合中重复主题是否过高。",
                "- 观察是否存在单一方向暴露过重。",
            ]
        )
    return "\n".join(
        [
            "#### 持有基金",
            *rows,
            "",
            "#### 组合观察",
            "- AI 组合复盘未完成，以下为规则版组合兜底复盘。",
            f"- 该账户持有 {count} 只场外基金，适合从组合层面观察风格暴露。",
            f"- 根据名称粗略识别风格：{themes}。",
            "",
            "#### 风格暴露",
            "- 如果同类主题较多，可能存在主题集中。",
            "- 如果主题较分散，整体更偏多主题分散配置。",
            "",
            "#### 配置节奏",
            "- 当前仅做组合层面观察，不做单只基金短线判断。",
            "- 后续应结合市场风格和组合集中度观察。",
            "",
            "#### 后续观察",
            "- 观察是否过度集中在单一主题。",
            "- 观察不同基金之间是否高度重叠。",
        ]
    )


def _review_or_rule_fallback(account: str, asset_type: str, review_text: str | None, holdings: list[dict]) -> str:
    if not review_text or _fund_review_looks_truncated(review_text):
        return _rule_based_fund_review(account, asset_type, holdings)
    return _fund_review_display_text(review_text)


def _portfolio_holdings_by_account(snapshot: dict, asset_type: str) -> dict[str, list[dict]]:
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(accounts, dict):
        return {}
    grouped: dict[str, list[dict]] = {}
    for account in _ordered_account_names(accounts):
        groups = accounts.get(account, {})
        if not isinstance(groups, dict):
            continue
        holdings = [item for item in groups.get(asset_type, []) or [] if isinstance(item, dict)]
        if holdings:
            grouped[str(account)] = holdings
    return grouped


def _replace_portfolio_section_for_public_html(
    markdown_text: str,
    snapshot: dict,
    asset_type: str,
    section_title: str,
) -> str:
    holdings_by_account = _portfolio_holdings_by_account(snapshot, asset_type)
    if not holdings_by_account:
        return markdown_text

    reviews = (
        _extract_lof_portfolio_reviews(markdown_text)
        if asset_type == "lof"
        else _extract_otc_portfolio_reviews(markdown_text)
    )
    lines = markdown_text.splitlines()
    start = None
    end = None
    for idx, line in enumerate(lines):
        heading = _heading_match(line)
        if heading is None:
            continue
        level, text = heading
        compact = re.sub(r"\s+", "", _clean_heading_text(text))
        if level == 2 and re.sub(r"\s+", "", section_title) in compact:
            start = idx
            end = len(lines)
            for next_idx in range(idx + 1, len(lines)):
                next_heading = _heading_match(lines[next_idx])
                if next_heading is not None and next_heading[0] <= 2:
                    end = next_idx
                    break
            break

    section_lines = [f"## {section_title}", ""]
    for account, holdings in holdings_by_account.items():
        section_lines.extend(
            [
                f"### {account}",
                "",
                _review_or_rule_fallback(account, asset_type, reviews.get(account), holdings),
                "",
            ]
        )
    replacement = "\n".join(section_lines).rstrip()

    if start is None or end is None:
        return markdown_text.rstrip() + "\n\n" + replacement + "\n"
    return "\n".join([*lines[:start], replacement, *lines[end:]])


def _sanitize_public_report_markdown(markdown_text: str, snapshot: dict) -> str:
    sanitized = _replace_portfolio_section_for_public_html(
        markdown_text,
        snapshot,
        "lof",
        "LOF/ETF 组合复盘",
    )
    return _replace_portfolio_section_for_public_html(
        sanitized,
        snapshot,
        "otc",
        "场外基金组合复盘",
    )


def _render_lof_portfolio_review(account: str, review_text: str | None, holdings: list[dict]) -> str:
    review_text = _fund_review_display_text(review_text)
    review_text = _review_or_rule_fallback(account, "lof", review_text, holdings)
    return f"""
<section class="panel portfolio-panel" data-review-type="lof">
  <h3>{escape(account)} LOF/ETF 组合复盘</h3>
  <p class="panel-intro">基于本账户全部场内基金进行组合层面观察。</p>
  <div class="report-fragment">{_render_markdown_fragment(review_text)}</div>
</section>
"""


def _render_otc_portfolio_review(account: str, review_text: str | None, holdings: list[dict]) -> str:
    review_text = _review_or_rule_fallback(account, "otc", review_text, holdings)
    return f"""
<section class="panel portfolio-panel" data-review-type="otc">
  <h3>{escape(account)} 场外基金组合复盘</h3>
  <p class="panel-intro">基于本账户全部场外基金进行组合层面观察。</p>
  <div class="report-fragment">{_render_markdown_fragment(review_text)}</div>
</section>
"""


def _fund_review_display_text(text: str) -> str:
    clean = text or ""
    clean = clean.replace(
        "本小节为 LOF/ETF 组合级复盘，不输出逐个标的观察或配置观察。",
        "本小节为 LOF/ETF 组合级复盘，仅做账户配置观察，不输出单只标的短线判断。",
    )
    for old, new in FUND_DECISION_WORD_REPLACEMENTS:
        clean = clean.replace(old, new)
    clean = clean.replace(
        "本小节为 LOF/ETF 组合级复盘，不输出逐个标的观察或配置观察。",
        "本小节为 LOF/ETF 组合级复盘，仅做账户配置观察，不输出单只标的短线判断。",
    )
    clean = clean.replace(
        "本小节为 LOF/ETF 组合级复盘，不输出逐个标的观察或配置观察",
        "本小节为 LOF/ETF 组合级复盘，仅做账户配置观察，不输出单只标的短线判断",
    )
    clean = clean.replace(
        "不输出逐个标的观察或配置观察",
        "仅做账户配置观察，不输出单只标的短线判断",
    )
    clean = clean.replace("不参与逐只个股交易结论", "不进行单只标的短线判断")
    clean = clean.replace("不参与逐只个股交易", "不进行单只标的短线判断")
    clean = clean.replace("个股式短线判断", "单只标的短线判断")
    return clean


def _normalize_fragment_heading(text: str) -> str:
    clean = _plain_markdown_text(text)
    clean = re.sub(r"^#+\s*", "", clean).strip()
    return clean


def _is_code_only_heading(text: str) -> bool:
    clean = _normalize_fragment_heading(text)
    return bool(re.fullmatch(r"\d{6}", clean))


def _should_skip_detail_section(heading_text: str) -> bool:
    clean = _normalize_fragment_heading(heading_text)
    if not clean or _is_code_only_heading(clean):
        return True
    return any(keyword in clean for keyword in SKIP_DETAIL_HEADING_KEYWORDS)


def _is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    if TABLE_SEPARATOR_RE.match(stripped):
        return True
    if TABLE_LINE_RE.match(stripped) and stripped.count("|") >= 2:
        return True
    return False


def _is_report_meta_line(line: str) -> bool:
    compact = _plain_markdown_text(line).lower()
    return any(keyword.lower() in compact for keyword in SKIP_DETAIL_LINE_KEYWORDS)


def _render_markdown_fragment(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    html: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_fence = False
    skip_section = False

    def inline(text: str) -> str:
        safe = escape(text.strip())
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        safe = re.sub(r"__(.+?)__", r"<strong>\1</strong>", safe)
        safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
        return safe

    def flush_paragraph() -> None:
        if paragraph:
            html.append(f"<p>{inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            html.append("<ul>" + "".join(f"<li>{inline(item)}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            flush_paragraph()
            flush_list()
            continue
        if in_fence:
            if stripped:
                paragraph.append(stripped)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        heading = _heading_match(stripped)
        if heading is not None:
            flush_paragraph()
            flush_list()
            level, text = heading
            clean_heading = _normalize_fragment_heading(text)
            skip_section = _should_skip_detail_section(clean_heading)
            if skip_section:
                continue
            tag = "h4" if level <= 3 else "h5"
            html.append(f"<{tag}>{inline(clean_heading)}</{tag}>")
            continue
        if skip_section:
            continue
        if _is_report_meta_line(stripped):
            flush_paragraph()
            flush_list()
            continue
        if _is_markdown_table_line(stripped):
            flush_paragraph()
            flush_list()
            continue
        bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
        if bullet:
            flush_paragraph()
            list_items.append(bullet.group(1).strip())
            continue
        ordered = re.match(r"^\d+[.)、]\s+(.+)$", stripped)
        if ordered:
            flush_paragraph()
            list_items.append(ordered.group(1).strip())
            continue
        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    return "".join(html) or '<p class="muted">暂无内容。</p>'


def _collect_named_fragment_section(markdown_text: str, keyword: str) -> str:
    lines = markdown_text.splitlines()
    collecting = False
    collected: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        heading = _heading_match(stripped)
        if heading is not None:
            heading_text = _normalize_fragment_heading(heading[1])
            if collecting:
                break
            collecting = keyword in heading_text
            continue
        if collecting:
            collected.append(raw_line)
    return "\n".join(collected).strip()


def _limit_fragment_body(markdown_text: str, max_lines: int) -> str:
    kept: list[str] = []
    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if _heading_match(stripped) is not None:
            continue
        if _is_report_meta_line(stripped) or _is_markdown_table_line(stripped):
            continue
        kept.append(stripped)
        if len([line for line in kept if line]) >= max_lines:
            break
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept).strip()


def _render_stock_brief_section(title: str, markdown_text: str, max_lines: int) -> str:
    body = _limit_fragment_body(markdown_text, max_lines=max_lines)
    if not body:
        return ""
    return f"""
<div class="stock-brief-section">
  <h5>{escape(title)}</h5>
  {_render_markdown_fragment(body)}
</div>
"""


def _first_fragment_body(markdown_text: str, max_lines: int = 2) -> str:
    lines: list[str] = []
    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _heading_match(stripped) is not None:
            continue
        if _is_report_meta_line(stripped) or _is_markdown_table_line(stripped):
            continue
        lines.append(stripped)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines).strip()


def _render_stock_brief(cleaned: str) -> str:
    sections = [
        _render_stock_brief_section("核心结论", _collect_named_fragment_section(cleaned, "核心结论"), 2),
        _render_stock_brief_section("作战计划", _collect_named_fragment_section(cleaned, "作战计划"), 3),
        _render_stock_brief_section("关联板块", _collect_named_fragment_section(cleaned, "关联板块"), 2),
    ]
    if not any(sections):
        fallback = _collect_named_fragment_section(cleaned, "重要信息速览") or _first_fragment_body(cleaned)
        sections = [_render_stock_brief_section("核心结论", fallback, 2)]
    return '<div class="stock-brief">' + "".join(section for section in sections if section) + "</div>"


def _sanitize_ai_snippet_for_holding(snippet: str, code: str) -> str:
    """Keep analysis text while dropping duplicate leading holding titles."""
    text = re.sub(r"^(#{1,6})(?=\S)", r"\1 ", snippet or "", flags=re.MULTILINE).strip()
    if not text:
        return ""

    lines = text.splitlines()
    while lines:
        first = lines[0].strip()
        heading = _heading_match(first)
        heading_text = _normalize_fragment_heading(heading[1]) if heading else _plain_markdown_text(first)
        ordered_title = _ordered_title_text(first) or ""
        if (
            code in _codes_in_text(heading_text)
            or _is_code_only_heading(heading_text)
            or (ordered_title and code in _codes_in_text(ordered_title))
        ):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def _render_text_snippets(snippets: list[str], code: str) -> str:
    if not snippets:
        return '<p class="muted">AI 暂未输出该标的分析，仅展示持仓清单。</p>'
    cleaned_snippets = []
    for snippet in snippets:
        cleaned = _sanitize_ai_snippet_for_holding(snippet, code)
        if cleaned:
            cleaned_snippets.append(cleaned)
    if not cleaned_snippets:
        return '<p class="muted">AI 暂未输出该标的分析，仅展示持仓清单。</p>'

    brief = _render_stock_brief(cleaned_snippets[0])
    full = "".join(
        f'<div class="report-fragment">{_render_markdown_fragment(cleaned)}</div>'
        for cleaned in cleaned_snippets
    )
    return f"""
{brief}
<details class="analysis-details">
  <summary>查看完整分析摘要</summary>
  <div class="details-body">{full}</div>
</details>
"""


def _summary_detail_text(summary: str, code: str) -> str:
    escaped_code = re.escape(code)
    text = _plain_markdown_text(summary)
    text = re.sub(r"^[-*+]\s+", "", text)
    text = re.sub(r"^\d+[.)、]\s+", "", text)
    text = re.sub(rf"^.*?[（(]\s*{escaped_code}\s*[）)]\s*[:：]?\s*", "", text)
    text = re.sub(rf"^{escaped_code}\s*[:：]\s*", "", text)
    text = text.replace("|", "｜")
    return text.strip() or "有 AI 摘要项，详见原始股票日报。"


def _failure_detail_text(failure: str, code: str) -> str:
    raw = failure or ""
    raw_lower = raw.lower()
    if (
        "503" in raw
        or "serviceunavailable" in raw_lower
        or "serviceunavailableerror" in raw_lower
        or "unavailable" in raw_lower
        or "high demand" in raw_lower
        or "overloaded" in raw_lower
        or "all llm models failed" in raw_lower
        or "geminiexception" in raw_lower
    ):
        return "Gemini 模型服务暂不可用，本标的未完成分析。"
    if (
        "429" in raw
        or "quota" in raw_lower
        or "resource_exhausted" in raw_lower
        or "resourceexhausted" in raw_lower
        or "too many requests" in raw_lower
        or "toomanyrequests" in raw_lower
        or "free tier requests limit" in raw_lower
    ):
        return "Gemini API 额度超限，导致本标的未完成分析。"
    if "timeout" in raw_lower or "timed out" in raw_lower:
        return "AI 请求超时，本标的未完成分析。"

    detail = _summary_detail_text(failure, code)
    detail = re.sub(r"^失败原因\s*", "", detail).strip()
    detail = re.sub(r"All LLM models failed.*", "", detail, flags=re.IGNORECASE).strip()
    detail = re.sub(r"\{.*", "", detail).strip()
    if len(detail) > 120:
        return detail[:120].rstrip() + "..."
    return detail or "本标的未完成分析。"


def _wrap_html(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3f6fa;
      --card: #ffffff;
      --text: #182235;
      --muted: #637086;
      --line: #dbe3ed;
      --line-strong: #c8d3e0;
      --link: #075fca;
      --link-hover: #044a9e;
      --soft: #f7f9fc;
      --primary-soft: #eaf2ff;
      --success: #087a5b;
      --warning: #966500;
      --danger: #b93832;
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-width: 0; background: var(--bg); }}
    body {{
      width: 100%;
      max-width: 1120px;
      min-width: 0;
      margin: 0 auto;
      padding: 24px 20px 44px;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.68 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Helvetica, Arial, sans-serif;
      overflow-wrap: anywhere;
      overflow-x: hidden;
    }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ color: var(--link-hover); text-decoration: underline; }}
    h1 {{ font-size: 30px; line-height: 1.28; margin: 0; }}
    h2 {{ font-size: 22px; line-height: 1.35; margin: 0; }}
    h3 {{ font-size: 18px; line-height: 1.45; margin: 0 0 14px; }}
    h4 {{ line-height: 1.45; }}
    .muted {{ color: var(--muted); }}
    .hero {{
      margin-bottom: 20px;
      padding: 24px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(24, 34, 53, 0.05);
    }}
    .hero-kicker {{
      display: block;
      margin-bottom: 6px;
      color: var(--link);
      font-size: 13px;
      font-weight: 700;
    }}
    .hero-copy {{ max-width: 760px; margin: 10px 0 0; color: var(--muted); }}
    .meta-row {{ display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 14px; color: var(--muted); }}
    .meta-row span {{ min-width: 0; }}
    .dashboard-section {{ margin-top: 26px; }}
    .section-heading {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 12px;
    }}
    .section-heading p {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .report-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .card, .report-card {{
      display: block;
      min-width: 0;
      padding: 18px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease;
    }}
    .card:hover, .report-card:hover {{
      text-decoration: none;
      border-color: #8aadd8;
      box-shadow: 0 8px 20px rgba(7, 95, 202, 0.08);
      transform: translateY(-1px);
    }}
    .report-card {{ min-height: 126px; }}
    .report-card.primary {{ border-top: 3px solid var(--link); }}
    .report-card-kicker {{ display: block; color: var(--muted); font-size: 13px; }}
    .report-card-title {{ display: block; margin: 8px 0 14px; color: var(--text); font-size: 18px; font-weight: 750; }}
    .report-card-action {{ color: var(--link); font-size: 14px; font-weight: 650; }}
    .card-title {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--text); font-size: 18px; font-weight: 750; }}
    .card-arrow {{ color: var(--link); font-size: 20px; font-weight: 500; }}
    .account-counts {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-top: 16px;
    }}
    .account-count {{ padding: 9px 8px; border-radius: 6px; background: var(--soft); text-align: center; }}
    .account-count strong {{ display: block; color: var(--text); font-size: 20px; line-height: 1.2; }}
    .account-count span {{ display: block; margin-top: 3px; color: var(--muted); font-size: 12px; }}
    .link-list {{ list-style: none; padding: 0; margin: 0; }}
    .link-list li {{
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }}
    .link-list li:last-child {{ border-bottom: 0; }}
    .panel {{
      margin-top: 16px;
      padding: 20px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .page-nav {{
      display: flex;
      align-items: center;
      gap: 14px;
      margin-bottom: 16px;
      padding: 0 2px 12px;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
    }}
    .account-nav {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 0 0 18px;
    }}
    .account-nav a {{
      padding: 7px 11px;
      color: var(--text);
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 14px;
    }}
    .account-section {{
      margin: 14px 0;
      background: var(--card);
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      overflow: hidden;
    }}
    .account-section > summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      list-style: none;
      cursor: pointer;
      padding: 15px 18px;
      background: var(--soft);
      border-bottom: 1px solid transparent;
    }}
    .account-section > summary::-webkit-details-marker {{ display: none; }}
    .account-section[open] > summary {{ border-bottom-color: var(--line); }}
    .account-section > summary::after {{ content: "+"; flex: 0 0 auto; color: var(--link); font-size: 22px; line-height: 1; }}
    .account-section[open] > summary::after {{ content: "−"; }}
    .account-summary-title {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; min-width: 0; }}
    .account-name {{ color: var(--text); font-size: 18px; font-weight: 750; }}
    .account-badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .count-badge, .type-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      color: var(--muted);
      background: #eef2f7;
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }}
    .account-content {{ padding: 0 20px 20px; }}
    .account-content > .panel {{
      margin: 0;
      padding: 22px 0;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      background: transparent;
    }}
    .account-content > .panel:last-child {{ border-bottom: 0; padding-bottom: 2px; }}
    .standalone-account {{ border: 1px solid var(--line-strong); border-radius: 8px; background: var(--card); }}
    .standalone-account .account-content {{ padding-top: 0; }}
    .panel-intro {{ margin: -6px 0 14px; color: var(--muted); font-size: 14px; }}
    .summary-list {{ list-style: none; padding: 0; margin: 0; }}
    .summary-item {{
      display: grid;
      grid-template-columns: minmax(230px, 0.72fr) minmax(0, 1.28fr);
      gap: 10px 18px;
      align-items: start;
      padding: 11px 0;
      border-bottom: 1px solid var(--line);
    }}
    .summary-item:last-child {{ border-bottom: 0; }}
    .summary-identity {{ display: flex; flex-wrap: wrap; align-items: center; gap: 5px 8px; min-width: 0; }}
    .summary-code {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
    .summary-result {{ min-width: 0; color: #344156; }}
    .summary-item[data-type="stock"] .type-badge {{ color: #075fca; background: #eaf2ff; }}
    .summary-item[data-type="lof"] .type-badge {{ color: #087a5b; background: #e9f7f2; }}
    .summary-item[data-type="otc"] .type-badge {{ color: #835b00; background: #fff5d6; }}
    .holding-list {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
    .holding-list.fund-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .holding-table-wrap {{ width: 100%; max-width: 100%; overflow-x: auto; }}
    table {{
      width: 100%;
      min-width: 620px;
      border-collapse: collapse;
      background: var(--card);
    }}
    th, td {{ border: 1px solid var(--line); padding: 8px 10px; text-align: left; }}
    th {{ background: var(--soft); }}
    .note {{
      margin: 12px 0;
      padding: 12px;
      background: #fff8dc;
      border: 1px solid #e5ca69;
      border-radius: 8px;
    }}
    .holding-item {{
      min-width: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--card);
    }}
    .holding-item[data-type="stock"] {{ border-left: 3px solid #6ca2e6; }}
    .holding-item[data-type="lof"] {{ border-left: 3px solid #55a98d; }}
    .holding-item[data-type="otc"] {{ border-left: 3px solid #c6a24c; }}
    .holding-head {{ display: flex; align-items: start; justify-content: space-between; gap: 12px; }}
    .holding-head h4 {{
      min-width: 0;
      margin: 0;
      font-size: 17px;
    }}
    .status-line {{
      margin: 11px 0 0;
      padding: 9px 11px;
      background: var(--soft);
      border-left: 3px solid var(--link);
      border-radius: 6px;
      overflow-wrap: anywhere;
    }}
    .holding-item[data-type="lof"] .status-line,
    .holding-item[data-type="otc"] .status-line {{ border-left: 0; color: var(--muted); }}
    .holding-analysis {{ margin-top: 12px; }}
    .report-fragment {{
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .report-fragment h4,
    .report-fragment h5 {{
      margin: 12px 0 6px;
      line-height: 1.4;
    }}
    .report-fragment p,
    .report-fragment ul {{
      margin: 8px 0;
    }}
    .stock-brief-section {{
      margin: 10px 0;
      padding: 8px 0 8px 12px;
      border-left: 2px solid #c7d8ee;
    }}
    .stock-brief-section h5 {{
      margin: 0 0 6px;
      font-size: 15px;
    }}
    .analysis-details {{
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .analysis-details summary {{
      padding: 9px 10px;
      font-size: 14px;
      font-weight: 650;
      background: var(--soft);
    }}
    .details-body {{ padding: 12px 14px 14px; }}
    .ai-snippet {{
      margin: 10px 0 0;
      padding: 10px;
      overflow-x: auto;
      white-space: pre-wrap;
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 6px;
      font: 14px/1.65 SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
    }}
    .steady-overview {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 16px;
    }}
    .steady-overview-item {{
      padding: 14px;
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .steady-overview-item strong {{ display: block; font-size: 22px; line-height: 1.25; }}
    .steady-overview-item span {{ display: block; margin-top: 4px; color: var(--muted); font-size: 13px; }}
    .steady-list {{ display: grid; grid-template-columns: 1fr; gap: 14px; }}
    .steady-card {{
      padding: 18px;
      background: var(--card);
      border: 1px solid var(--line);
      border-left: 4px solid var(--success);
      border-radius: 8px;
    }}
    .steady-card[data-risk-tier="较稳健"] {{ border-left-color: #65934d; }}
    .steady-card[data-risk-tier="观察"],
    .steady-card[data-risk-tier="数据不足"] {{ border-left-color: var(--warning); }}
    .steady-card[data-risk-tier="不纳入"] {{ border-left-color: var(--danger); }}
    .steady-card-head {{ display: flex; align-items: start; justify-content: space-between; gap: 14px; }}
    .steady-card-head h3 {{ margin: 0; }}
    .risk-badge {{
      flex: 0 0 auto;
      padding: 4px 9px;
      color: var(--success);
      background: #e9f7f2;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 750;
    }}
    .steady-card[data-risk-tier="观察"] .risk-badge,
    .steady-card[data-risk-tier="数据不足"] .risk-badge {{ color: var(--warning); background: #fff5d6; }}
    .steady-card[data-risk-tier="不纳入"] .risk-badge {{ color: var(--danger); background: #fdeceb; }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-top: 14px;
    }}
    .metric {{ min-width: 0; padding: 10px; background: var(--soft); border-radius: 6px; }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 2px; font-size: 15px; font-variant-numeric: tabular-nums; }}
    .steady-columns {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }}
    .steady-block {{ min-width: 0; padding-top: 12px; border-top: 1px solid var(--line); }}
    .steady-block h4 {{ margin: 0 0 7px; font-size: 14px; }}
    .steady-block ul {{ margin: 0; padding-left: 20px; }}
    .steady-block li {{ margin: 4px 0; }}
    .replay-list {{ display: flex; flex-wrap: wrap; gap: 7px; margin-top: 8px; }}
    .replay-pill {{ padding: 5px 8px; border-radius: 6px; background: var(--soft); font-size: 13px; }}
    .replay-pill.positive {{ color: var(--success); }}
    .replay-pill.negative {{ color: var(--danger); }}
    .steady-excluded {{ margin-top: 16px; }}
    .steady-excluded > summary {{ cursor: pointer; font-weight: 700; }}
    .disclaimer {{
      margin-top: 30px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 14px;
    }}
    .archive-details {{
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--card);
      overflow: hidden;
    }}
    .archive-details > summary {{ padding: 13px 16px; cursor: pointer; font-weight: 700; background: var(--soft); }}
    .raw-report {{ max-width: 100%; overflow-x: auto; }}
    .raw-report pre {{ max-width: 100%; overflow-x: auto; white-space: pre-wrap; }}
    @media (max-width: 720px) {{
      body {{ padding: 12px; font-size: 15px; }}
      h1 {{ font-size: 25px; }}
      h2 {{ font-size: 20px; }}
      .hero {{ padding: 18px; }}
      .section-heading {{ display: block; }}
      .section-heading p {{ margin-top: 4px; }}
      .grid, .report-grid {{ grid-template-columns: 1fr; }}
      .card, .report-card, .panel {{ padding: 15px; }}
      .report-card {{ min-height: 0; }}
      .account-counts {{ gap: 6px; }}
      .account-section > summary {{ align-items: start; padding: 13px 14px; }}
      .account-name {{ font-size: 17px; }}
      .account-content {{ padding: 0 14px 14px; }}
      .account-content > .panel {{ padding: 18px 0; }}
      .summary-item {{ grid-template-columns: 1fr; gap: 5px; }}
      .holding-list.fund-grid {{ grid-template-columns: 1fr; }}
      .holding-item {{ padding: 14px; }}
      .holding-head {{ display: block; }}
      .holding-head .type-badge {{ margin-top: 7px; }}
      .steady-overview {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }}
      .steady-overview-item {{ padding: 10px 7px; }}
      .steady-overview-item strong {{ font-size: 19px; }}
      .steady-overview-item span {{ font-size: 11px; }}
      .metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .steady-columns {{ grid-template-columns: 1fr; }}
      .steady-card {{ padding: 15px; }}
      .steady-card-head {{ display: block; }}
      .steady-card-head .risk-badge {{ display: inline-flex; margin-top: 8px; }}
      th, td {{ padding: 7px 8px; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _enhance_report_html(html: str, title: str) -> str:
    """Add mobile viewport, Pages-friendly CSS, a home link, and disclaimer."""
    body_start = f"""
            <nav class="page-nav"><a href="../index.html">返回首页</a></nav>
            <header class="hero">
                <span class="hero-kicker">大盘复盘</span>
                <h1>{escape(title)}</h1>
                <div class="meta-row"><span>生成时间：{escape(_now_text())}</span></div>
            </header>
            """
    footer = f"""
            <footer class="disclaimer">{escape(DISCLAIMER)}</footer>
            """

    if "<head>" in html and "<meta charset=" in html:
        html = re.sub(
            r'(<meta charset=["\']?utf-8["\']?>)',
            r'\1\n            <meta name="viewport" content="width=device-width, initial-scale=1">',
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    if "<head>" in html and "<title>" not in html.lower():
        html = html.replace("<head>", f"<head>\n            <title>{escape(title)}</title>", 1)
    if "</style>" in html:
        html = html.replace(
            "</style>",
            """
            *, *::before, *::after { box-sizing: border-box; }
            html { width: 100%; max-width: 100%; min-width: 0; background: #f3f6fa; overflow-x: hidden; }
            body { width: 100%; max-width: 1120px; min-width: 0; margin: 0 auto; padding: 24px 20px 44px; line-height: 1.68; overflow-wrap: anywhere; overflow-x: hidden; }
            body > * { max-width: 100%; min-width: 0; }
            .page-nav { margin-bottom: 16px; padding: 0 2px 12px; border-bottom: 1px solid #dbe3ed; font-size: 14px; }
            .hero { margin-bottom: 20px; padding: 24px; background: #fff; border: 1px solid #dbe3ed; border-radius: 8px; box-shadow: 0 8px 24px rgba(24,34,53,.05); }
            .hero-kicker { display: block; margin-bottom: 6px; color: #075fca; font-size: 13px; font-weight: 700; }
            .meta-row { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 14px; color: #637086; }
            table { display: block; max-width: 100%; overflow-x: auto; white-space: nowrap; }
            img, svg, canvas { max-width: 100%; height: auto; }
            @media (max-width: 720px) { body { width: 100%; padding: 12px; font-size: 15px; } .hero { padding: 18px; } h1 { font-size: 25px; } }
            </style>""",
            1,
        )
    if "<body>" in html:
        html = re.sub(r"<h1\b[^>]*>.*?</h1>\s*", "", html, count=1, flags=re.IGNORECASE | re.DOTALL)
        html = html.replace("<body>", f"<body>\n            {body_start}", 1)
    if "</body>" in html:
        html = html.replace("</body>", f"{footer}\n        </body>", 1)
    return html


def _render_raw_report_details(raw_report_html: str) -> str:
    return f"""
<details class="archive-details">
  <summary>原始 AI 股票日报</summary>
  <div class="details-body raw-report">
    {raw_report_html}
  </div>
</details>
"""


def _render_unmatched_ai_details(unmatched: dict[str, list[str]]) -> str:
    if not unmatched:
        return """
<details class="archive-details">
  <summary>未匹配 AI 报告项</summary>
  <div class="details-body">
    <p class="muted">暂无未匹配内容。</p>
  </div>
</details>
"""
    rows = []
    for code, snippets in sorted(unmatched.items()):
        snippet_html = "".join(f'<pre class="ai-snippet">{escape(snippet)}</pre>' for snippet in snippets)
        rows.append(f"<h3>{escape(code)}</h3>{snippet_html}")
    return f"""
<details class="archive-details">
  <summary>未匹配 AI 报告项</summary>
  <div class="details-body">
    <p class="muted">以下内容来自 AI 原始报告，但未在当前 holdings_data.json 持仓中匹配到，仅供排查。</p>
    {''.join(rows)}
  </div>
</details>
"""


def _render_summary_fallback(summary_by_code: dict[str, list[str]], code: str) -> str:
    summaries = summary_by_code.get(code, [])
    if not summaries:
        return '<p class="muted">AI 暂未输出该标的分析，仅展示持仓清单。</p>'
    snippets = []
    for summary in summaries[:MAX_AI_SNIPPETS_PER_CODE]:
        detail = _summary_detail_text(summary, code)
        snippets.append(f'<p class="note">{escape(detail)}</p>')
    return (
        '<p class="muted">暂无完整单项分析，以下为 AI 摘要：</p>'
        + "".join(snippets)
    )


def _holding_status_text(
    code: str,
    asset_type: str,
    account: str,
    summary_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
    otc_reviews_by_account: dict[str, str],
) -> str:
    if asset_type == "stock" and summary_by_code.get(code):
        return _summary_detail_text(summary_by_code[code][0], code)
    if asset_type == "stock" and unfinished_by_code.get(code):
        return "分析失败：" + _failure_detail_text(unfinished_by_code[code][0], code)
    if asset_type == "lof":
        return "本标的属于 LOF/ETF，已纳入账户级 LOF/ETF 组合复盘，不进行单只标的短线判断。"
    if asset_type == "otc":
        if otc_reviews_by_account.get(account):
            return "本标的属于场外基金，已纳入账户级基金组合复盘，不进行单只基金短线判断。"
        return "场外基金暂未接入股票日报分析，仅展示持仓清单。"
    return "暂无摘要，仅展示持仓清单。"


def _holding_analysis_card(
    item: dict,
    snippets_by_code: dict[str, list[str]],
    summary_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
    otc_reviews_by_account: dict[str, str],
) -> str:
    code = str(item.get("code", "") or "-")
    name = _display_holding_name(item.get("name", ""), code)
    account = str(item.get("account", "") or "")
    asset_type = str(item.get("type", "") or "")
    label = TYPE_LABELS.get(asset_type, asset_type or "-")
    if asset_type == "otc":
        if otc_reviews_by_account.get(account):
            analysis = ""
        else:
            analysis = (
                '<p class="note">场外基金暂未接入股票日报分析。本页仅展示来自 '
                "stock-dashboard 的最新持仓清单，后续可接入基金净值、重仓行业和基金经理复盘。</p>"
            )
    elif asset_type == "lof":
        analysis = ""
    elif snippets_by_code.get(code):
        analysis = _render_text_snippets(snippets_by_code.get(code, []), code)
    elif unfinished_by_code.get(code):
        analysis = _render_failure_snippets(unfinished_by_code.get(code, []), code)
    else:
        analysis = _render_summary_fallback(summary_by_code, code)
    status = _holding_status_text(
        code,
        asset_type,
        account,
        summary_by_code,
        unfinished_by_code,
        otc_reviews_by_account,
    )
    analysis_html = f'<div class="holding-analysis">{analysis}</div>' if analysis else ""
    return f"""
<article class="holding-item" data-account="{escape(account, quote=True)}" data-code="{escape(code, quote=True)}" data-type="{escape(asset_type, quote=True)}">
  <div class="holding-head">
    <h4>{escape(name)}（{escape(code)}）</h4>
    <span class="type-badge">{escape(label)}</span>
  </div>
  <p class="status-line">{escape(status)}</p>
  {analysis_html}
</article>
"""


def _render_holding_cards(
    items: list[dict],
    snippets_by_code: dict[str, list[str]],
    summary_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
    otc_reviews_by_account: dict[str, str],
) -> str:
    if not items:
        return '<p class="muted">暂无持仓。</p>'
    cards = "".join(
        _holding_analysis_card(
            item,
            snippets_by_code,
            summary_by_code,
            unfinished_by_code,
            otc_reviews_by_account,
        )
        for item in items
    )
    fund_only = all(str(item.get("type", "")) in {"lof", "otc"} for item in items)
    grid_class = "holding-list fund-grid" if fund_only else "holding-list"
    return f'<div class="{grid_class}">{cards}</div>'


def _render_account_summary(
    account: str,
    items: list[dict],
    summary_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
    otc_reviews_by_account: dict[str, str],
) -> str:
    rows = []
    for item in items:
        code = str(item.get("code", "") or "").strip()
        name = _display_holding_name(item.get("name", ""), code)
        asset_type = str(item.get("type", "") or "")
        label = TYPE_LABELS.get(asset_type, asset_type or "-")
        summaries = summary_by_code.get(code, [])
        if asset_type == "otc":
            if otc_reviews_by_account.get(account):
                detail = "已纳入账户级基金组合复盘。"
            else:
                detail = "场外基金暂未接入股票日报分析，仅展示持仓清单。"
        elif asset_type == "lof":
            detail = "已纳入账户级组合复盘。"
        elif summaries:
            detail = "；".join(
                _summary_detail_text(summary, code)
                for summary in summaries[:MAX_AI_SNIPPETS_PER_CODE]
            )
        elif unfinished_by_code.get(code):
            detail = "分析失败：" + "；".join(
                _failure_detail_text(failure, code)
                for failure in unfinished_by_code.get(code, [])[:MAX_AI_SNIPPETS_PER_CODE]
            )
        else:
            detail = "暂无摘要，仅展示持仓清单。"
        rows.append(
            f'<li class="summary-item" data-account="{escape(account, quote=True)}" '
            f'data-code="{escape(code, quote=True)}" data-type="{escape(asset_type, quote=True)}">'
            '<div class="summary-identity">'
            f"<strong>{escape(name)}</strong>"
            f'<span class="summary-code">{escape(code)}</span>'
            f'<span class="type-badge">{escape(label)}</span>'
            "</div>"
            f'<div class="summary-result">{escape(detail)}</div>'
            "</li>"
        )
    body = f'<ul class="summary-list">{"".join(rows)}</ul>' if rows else '<p class="muted">暂无持仓。</p>'
    return f"""
<section class="panel account-analysis-summary">
  <h3>{escape(account)}分析结果摘要</h3>
  <p class="panel-intro">每项持仓都在这里有明确状态，详细内容紧随其后。</p>
  {body}
</section>
"""


def _account_count_badges(items: list[dict]) -> str:
    counts = {"stock": 0, "lof": 0, "otc": 0}
    for item in items:
        asset_type = str(item.get("type", ""))
        if asset_type in counts:
            counts[asset_type] += 1
    labels = (("stock", "A股"), ("lof", "LOF/ETF"), ("otc", "场外基金"))
    return "".join(
        f'<span class="count-badge">{label} {counts[key]}</span>'
        for key, label in labels
        if counts[key]
    )


def _render_account_content(
    account: str,
    items: list[dict],
    summary_by_code: dict[str, list[str]],
    snippets_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
    lof_reviews_by_account: dict[str, str],
    otc_reviews_by_account: dict[str, str],
) -> str:
    lof_items = [item for item in items if str(item.get("type", "")) == "lof"]
    otc_items = [item for item in items if str(item.get("type", "")) == "otc"]
    lof_review = _render_lof_portfolio_review(account, lof_reviews_by_account.get(account), lof_items) if lof_items else ""
    otc_review = _render_otc_portfolio_review(account, otc_reviews_by_account.get(account), otc_items) if otc_items else ""
    return f"""
    {_render_account_summary(account, items, summary_by_code, unfinished_by_code, otc_reviews_by_account)}
    <section class="panel holdings-panel">
      <h3>{escape(account)}持仓明细与分析</h3>
      <p class="panel-intro">名称、代码、账户和类型来自最新公开持仓快照；AI 只补充分析文本。</p>
      {_render_holding_cards(items, snippets_by_code, summary_by_code, unfinished_by_code, otc_reviews_by_account)}
    </section>
    {lof_review}
    {otc_review}
"""


def _render_standard_account_section(
    account: str,
    groups: dict,
    summary_by_code: dict[str, list[str]],
    snippets_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
    lof_reviews_by_account: dict[str, str],
    otc_reviews_by_account: dict[str, str],
    is_open: bool,
    standalone: bool = False,
) -> str:
    items = _account_items(groups)
    content = _render_account_content(
        account,
        items,
        summary_by_code,
        snippets_by_code,
        unfinished_by_code,
        lof_reviews_by_account,
        otc_reviews_by_account,
    )
    anchor = _account_slug(account)
    if standalone:
        return f"""
<section class="account-section standalone-account" id="account-{escape(anchor, quote=True)}" data-account="{escape(account, quote=True)}">
  <div class="account-content">{content}</div>
</section>
"""
    return f"""
<details class="account-section" id="account-{escape(anchor, quote=True)}" data-account="{escape(account, quote=True)}" {"open" if is_open else ""}>
  <summary>
    <span class="account-summary-title">
      <span class="account-name">{escape(account)}</span>
      <span class="account-badges">{_account_count_badges(items)}</span>
    </span>
  </summary>
  <div class="account-content">{content}</div>
</details>
"""


def _build_report_context(
    markdown_text: str,
    snapshot: dict,
) -> HoldingReportContext:
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    account_names = _ordered_account_names(accounts)
    all_holdings: list[dict] = []
    if isinstance(accounts, dict):
        for account in account_names:
            groups = accounts.get(account, {})
            all_holdings.extend(_account_items(groups))

    snippets_by_code = _extract_ai_snippets(markdown_text, all_holdings)
    summary_by_code = _extract_ai_summary_items(markdown_text)
    unfinished_by_code = _extract_unfinished_items(markdown_text)
    lof_reviews_by_account = _extract_lof_portfolio_reviews(markdown_text)
    otc_reviews_by_account = _extract_otc_portfolio_reviews(markdown_text)
    snapshot_codes = _all_snapshot_codes(snapshot)
    unmatched: dict[str, list[str]] = {}
    for source in (summary_by_code, snippets_by_code, unfinished_by_code):
        for code, snippets in source.items():
            if code in snapshot_codes:
                continue
            for snippet in snippets:
                _append_ai_snippet(unmatched, code, snippet)
    return HoldingReportContext(
        summary_by_code=summary_by_code,
        snippets_by_code=snippets_by_code,
        unfinished_by_code=unfinished_by_code,
        lof_reviews_by_account=lof_reviews_by_account,
        otc_reviews_by_account=otc_reviews_by_account,
        unmatched=unmatched,
    )


def _empty_report_context() -> HoldingReportContext:
    return HoldingReportContext(
        summary_by_code={},
        snippets_by_code={},
        unfinished_by_code={},
        lof_reviews_by_account={},
        otc_reviews_by_account={},
        unmatched={},
    )


def _build_holding_report_page(
    title: str,
    markdown_text: str,
    raw_report_html: str,
    snapshot: dict,
) -> str:
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    account_names = _ordered_account_names(accounts)
    context = _build_report_context(markdown_text, snapshot)
    sections = []
    for idx, account in enumerate(account_names):
        groups = accounts.get(account, {}) if isinstance(accounts, dict) else {}
        if not isinstance(groups, dict):
            groups = {}
        sections.append(
            _render_standard_account_section(
                account,
                groups,
                context.summary_by_code,
                context.snippets_by_code,
                context.unfinished_by_code,
                context.lof_reviews_by_account,
                context.otc_reviews_by_account,
                idx == 0,
            )
        )

    holding_count = sum(
        len(_account_items(groups))
        for groups in accounts.values()
        if isinstance(groups, dict)
    )
    account_links = "".join(
        f'<a href="#account-{escape(_account_slug(account), quote=True)}">{escape(account)}</a>'
        for account in account_names
    )

    body = f"""
<nav class="page-nav"><a href="../index.html">返回首页</a></nav>
<header class="hero">
  <span class="hero-kicker">最新持仓日报</span>
  <h1>{escape(title)}</h1>
  <div class="meta-row">
    <span>生成时间：{escape(_now_text())}</span>
    <span>{len(account_names)} 个账户 · {holding_count} 项持仓</span>
  </div>
  <p class="hero-copy">持仓身份以 stock-dashboard 最新公开快照为准。账户摘要先交代全部持仓状态，随后给出逐项明细与账户级基金组合复盘。</p>
</header>
<nav class="account-nav" aria-label="账户快速导航">{account_links}</nav>
{''.join(sections)}
{_render_raw_report_details(raw_report_html)}
{_render_unmatched_ai_details(context.unmatched)}
<footer class="disclaimer">{escape(DISCLAIMER)}</footer>
"""
    return _wrap_html(title, body)


def _discover_reports() -> list[Path]:
    if not REPORTS_DIR.exists():
        print(f"No reports directory found: {REPORTS_DIR}")
        return []
    reports = sorted(REPORTS_DIR.glob("*.md"))
    if not reports:
        print(f"No Markdown reports found under: {REPORTS_DIR}")
    return reports


def _build_report_pages(snapshot: dict) -> list[ReportPage]:
    report_paths = _discover_reports()
    renderer = _load_markdown_renderer() if report_paths else None
    SITE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    pages: list[ReportPage] = []
    for report_path in report_paths:
        markdown_text = report_path.read_text(encoding="utf-8")
        title = _friendly_report_title(report_path, markdown_text)
        kind = _report_kind(report_path)
        output_path = SITE_REPORTS_DIR / _html_name(report_path)
        display_markdown_text = (
            _sanitize_public_report_markdown(markdown_text, snapshot)
            if kind == "stock"
            else markdown_text
        )
        raw_html = renderer(display_markdown_text) if renderer else _wrap_html(title, escape(display_markdown_text))
        if kind == "stock":
            html = _build_holding_report_page(title, display_markdown_text, raw_html, snapshot)
        else:
            html = _enhance_report_html(raw_html, title)
        output_path.write_text(html, encoding="utf-8")
        pages.append(
            ReportPage(
                source=report_path,
                output=output_path,
                title=title,
                kind=kind,
                sort_key=(
                    _extract_date_key(report_path),
                    report_path.stat().st_mtime,
                    report_path.name,
                ),
            )
        )

    pages.sort(key=lambda page: page.sort_key, reverse=True)
    return pages


def _build_account_page(
    account: str,
    groups: dict,
    latest_stock_report: ReportPage | None,
    context: HoldingReportContext,
) -> str:
    report_link = '<span class="muted">本次构建未发现 report_*.md，最新持仓日报暂不可用。</span>'
    if latest_stock_report:
        href = f"../{_relative_href(latest_stock_report.output)}"
        report_link = f'<a href="{escape(href)}">查看最新持仓日报 →</a>'

    body = f"""
<nav class="page-nav"><a href="../index.html">返回首页</a></nav>
<header class="hero">
  <span class="hero-kicker">账户持仓复盘</span>
  <h1>{escape(account)}持仓复盘</h1>
  <p class="hero-copy">本页与总持仓日报共用同一套账户内容组件，仅展示公开持仓字段。</p>
  <div class="meta-row">{report_link}</div>
</header>
{_render_standard_account_section(
    account,
    groups,
    context.summary_by_code,
    context.snippets_by_code,
    context.unfinished_by_code,
    context.lof_reviews_by_account,
    context.otc_reviews_by_account,
    True,
    True,
)}
<footer class="disclaimer">{escape(DISCLAIMER)}</footer>
"""
    return _wrap_html(f"{account}持仓复盘", body)


def _build_account_pages(snapshot: dict, pages: list[ReportPage]) -> list[AccountPage]:
    SITE_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    latest_stock_report = _latest_report(pages, "stock")
    context = _empty_report_context()
    if latest_stock_report and latest_stock_report.source.exists():
        try:
            context = _build_report_context(
                latest_stock_report.source.read_text(encoding="utf-8"),
                snapshot,
            )
        except Exception as exc:
            print(f"Failed to build account page report context: {type(exc).__name__}: {exc}")
    account_pages: list[AccountPage] = []

    ordered_accounts = sorted(
        accounts.items(),
        key=lambda item: (
            ACCOUNT_ORDER.index(item[0]) if item[0] in ACCOUNT_ORDER else len(ACCOUNT_ORDER),
            item[0],
        ),
    )

    for account, groups in ordered_accounts:
        if not isinstance(groups, dict):
            continue
        output_path = SITE_ACCOUNTS_DIR / f"{_account_slug(str(account))}.html"
        output_path.write_text(
            _build_account_page(str(account), groups, latest_stock_report, context),
            encoding="utf-8",
        )
        account_pages.append(
            AccountPage(
                account=str(account),
                output=output_path,
                counts=_counts_for_account(groups),
            )
        )

    return account_pages


def _steady_number(value: object, *, suffix: str = "", decimals: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "数据不足"
    return f"{number:.{decimals}f}{suffix}"


def _steady_metric(label: str, value: str) -> str:
    return (
        '<div class="metric">'
        f'<span>{escape(label)}</span><strong>{escape(value)}</strong>'
        "</div>"
    )


def _steady_bullet_block(title: str, items: list[object], empty_text: str) -> str:
    clean_items = [str(item).strip() for item in items if str(item).strip()]
    if not clean_items:
        clean_items = [empty_text]
    bullets = "".join(f"<li>{escape(item)}</li>" for item in clean_items)
    return f'<section class="steady-block"><h4>{escape(title)}</h4><ul>{bullets}</ul></section>'


def _steady_replay(result: dict) -> str:
    periods = result.get("replay_periods") if isinstance(result.get("replay_periods"), list) else []
    if not periods:
        return '<p class="muted">完整年度数据不足。</p>'
    pills = []
    for item in periods:
        if not isinstance(item, dict):
            continue
        try:
            number = float(item.get("total_return_pct"))
        except (TypeError, ValueError):
            continue
        state = "positive" if number >= 0 else "negative"
        pills.append(
            f'<span class="replay-pill {state}">'
            f'{escape(str(item.get("label") or "-"))}：{number:+.2f}%</span>'
        )
    return f'<div class="replay-list">{"".join(pills)}</div>' if pills else '<p class="muted">完整年度数据不足。</p>'


def _steady_price_bands(result: dict) -> str:
    bands = result.get("price_bands") if isinstance(result.get("price_bands"), dict) else {}
    if not bands:
        return '<p class="muted">现金分红证据不足，无法计算。</p>'
    return (
        '<div class="metric-grid">'
        + _steady_metric("约 5% 股息率", _steady_number(bands.get("high_income_price"), suffix=" 元"))
        + _steady_metric("约 3.5% 股息率", _steady_number(bands.get("balanced_price"), suffix=" 元"))
        + _steady_metric("约 2.5% 股息率", _steady_number(bands.get("low_income_price"), suffix=" 元"))
        + "</div>"
    )


def _render_steady_result(result: dict, *, compact: bool = False) -> str:
    code = str(result.get("code") or "-")
    name = str(result.get("name") or code)
    risk_tier = str(result.get("risk_tier") or "数据不足")
    market = str(result.get("market") or ("沪市" if code.startswith("6") else "深市"))
    identity_meta = f"{market} · 全市场深度评估"
    risks = result.get("risks") if isinstance(result.get("risks"), list) else []

    if compact:
        reason = str(risks[0]) if risks else "未达到低风险候选门槛"
        return f"""
<article class="steady-card" data-risk-tier="{escape(risk_tier)}" data-code="{escape(code)}">
  <div class="steady-card-head">
    <div><h3>{escape(name)}（{escape(code)}）</h3><p class="muted">{escape(identity_meta)}</p></div>
    <span class="risk-badge">{escape(risk_tier)}</span>
  </div>
  <p>{escape(reason)}</p>
</article>
"""

    metrics = "".join(
        (
            _steady_metric(
                "最近有效价格",
                _steady_number(result.get("current_price"), suffix=" 元")
                + (f" · {result.get('price_date')}" if result.get("price_date") else ""),
            ),
            _steady_metric("TTM 税前股息率", _steady_number(result.get("ttm_dividend_yield_pct"), suffix="%")),
            _steady_metric("连续现金分红", f"{int(result.get('consecutive_dividend_years') or 0)} 年"),
            _steady_metric("分红可持续性", str(result.get("dividend_sustainability") or "数据不足")),
            _steady_metric("近年最大回撤", _steady_number(result.get("max_drawdown_pct"), suffix="%")),
            _steady_metric("年化波动率", _steady_number(result.get("annualized_volatility_pct"), suffix="%")),
        )
    )
    strengths = result.get("strengths") if isinstance(result.get("strengths"), list) else []
    return f"""
<article class="steady-card" data-risk-tier="{escape(risk_tier)}" data-code="{escape(code)}">
  <div class="steady-card-head">
    <div><h3>{escape(name)}（{escape(code)}）</h3><p class="muted">{escape(identity_meta)}</p></div>
    <span class="risk-badge">{escape(risk_tier)}</span>
  </div>
  <div class="metric-grid">{metrics}</div>
  <div class="steady-columns">
    {_steady_bullet_block("通过证据", strengths, "暂无足够证据")}
    {_steady_bullet_block("主要风险", risks, "未发现触发硬门槛的异常")}
  </div>
  <section class="steady-block"><h4>股息率对应价格</h4>{_steady_price_bands(result)}</section>
  <section class="steady-block"><h4>最近五期价格与分红复权回放</h4>{_steady_replay(result)}</section>
</article>
"""


def _build_steady_income_page(payload: dict) -> str:
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    excluded = payload.get("excluded") if isinstance(payload.get("excluded"), list) else []
    evaluated_count = int(payload.get("evaluated_count") or 0)
    qualified_count = int(payload.get("qualified_count") or 0)
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    stats = payload.get("screening_stats") if isinstance(payload.get("screening_stats"), dict) else {}
    universe_count = int(stats.get("universe_count") or universe.get("count") or 0)
    prefilter_count = int(stats.get("prefilter_eligible_count") or 0)
    as_of = str(payload.get("as_of") or "尚未生成")

    if candidates:
        candidate_html = '<div class="steady-list">' + "".join(
            _render_steady_result(item) for item in candidates if isinstance(item, dict)
        ) + "</div>"
    else:
        candidate_html = (
            '<div class="note"><strong>本次全市场深度评估没有标的通过低风险硬门槛。</strong>'
            '<div class="muted">这不是收益判断，而是风险、分红或数据证据尚不足。</div></div>'
        )

    excluded_html = "".join(
        _render_steady_result(item, compact=True) for item in excluded if isinstance(item, dict)
    )
    if not excluded_html:
        excluded_html = '<p class="muted">暂无未纳入标的。</p>'

    body = f"""
<nav class="page-nav"><a href="index.html">返回首页</a></nav>
<header class="hero">
  <span class="hero-kicker">沪深全市场 · 低风险优先</span>
  <h1>稳健收益</h1>
  <p class="hero-copy">覆盖全部沪深 A 股，先做分红、盈利和上市年限预筛，再用现金流、分红连续性、回撤与波动做深度硬门槛。高股息不能覆盖高风险。</p>
  <div class="meta-row"><span>评估基准日：{escape(as_of)}</span><span>生成时间：{escape(str(payload.get('generated_at') or _now_text()))}</span></div>
  <div class="steady-overview">
    <div class="steady-overview-item"><strong>{universe_count}</strong><span>沪深全市场覆盖</span></div>
    <div class="steady-overview-item"><strong>{prefilter_count}</strong><span>通过基础预筛</span></div>
    <div class="steady-overview-item"><strong>{evaluated_count}</strong><span>进入深度评估</span></div>
    <div class="steady-overview-item"><strong>{qualified_count}</strong><span>通过低风险硬门槛</span></div>
  </div>
</header>
<main id="steady-income-results" data-universe-count="{universe_count}" data-prefilter-count="{prefilter_count}" data-evaluated-count="{evaluated_count}" data-qualified-count="{qualified_count}">
  <section class="dashboard-section">
    <div class="section-heading"><div><h2>低风险候选</h2><p>只展示“稳健”或“较稳健”层级；规则分不能跨风险层升级标的。</p></div></div>
    {candidate_html}
  </section>
  <details class="archive-details steady-excluded">
    <summary>查看深度评估后未通过硬门槛的 {len(excluded)} 只标的</summary>
    <div class="details-body steady-list">{excluded_html}</div>
  </details>
  <section class="panel">
    <h2>判定边界</h2>
    <ul>
      <li>股票池覆盖全部沪深 A 股，不读取当前持仓作为候选范围；北交所、B 股、LOF/ETF 与场外基金不参与。</li>
      <li>全市场基础预筛覆盖每只股票；深度评估只处理预筛排名靠前的有限候选，并公开各阶段数量。</li>
      <li>风险硬门槛优先，规则分仅在同一风险层内排序。</li>
      <li>股息率按 TTM 税前现金分红与最近有效收盘价计算。</li>
      <li>历史回放使用前复权行情；不预测未来分红，不承诺收益。</li>
    </ul>
  </section>
</main>
<footer class="disclaimer">本页面用于低风险现金流与长期总回报复盘，不构成投资建议。</footer>
"""
    return _wrap_html("稳健收益", body)


def _reports_index_block(pages: list[ReportPage]) -> str:
    latest_stock = _latest_report(pages, "stock")
    latest_market = _latest_report(pages, "market")
    items = []
    if latest_stock:
        items.append(
            f'<a class="report-card primary" href="{escape(_relative_href(latest_stock.output))}">'
            '<span class="report-card-kicker">最新持仓日报</span>'
            f'<strong class="report-card-title">{escape(latest_stock.title)}</strong>'
            '<span class="report-card-action">查看持仓日报 →</span></a>'
        )
    else:
        items.append('<div class="report-card"><span class="report-card-kicker">最新持仓日报</span><strong class="report-card-title">暂无</strong></div>')
    if latest_market:
        items.append(
            f'<a class="report-card" href="{escape(_relative_href(latest_market.output))}">'
            '<span class="report-card-kicker">大盘复盘</span>'
            f'<strong class="report-card-title">{escape(latest_market.title)}</strong>'
            '<span class="report-card-action">查看大盘复盘 →</span></a>'
        )
    else:
        items.append('<div class="report-card"><span class="report-card-kicker">大盘复盘</span><strong class="report-card-title">暂无</strong></div>')
    items.append(
        '<a class="report-card" href="advice_backtest.html">'
        '<span class="report-card-kicker">模型表现</span>'
        '<strong class="report-card-title">AI 建议准确性回测</strong>'
        '<span class="report-card-action">查看历史命中率 →</span></a>'
    )
    items.append(
        '<a class="report-card" href="steady_income.html">'
        '<span class="report-card-kicker">沪深全市场 · 低风险现金流</span>'
        '<strong class="report-card-title">稳健收益</strong>'
        '<span class="report-card-action">查看全市场风险优先筛选 →</span></a>'
    )
    return (
        '<section class="dashboard-section">'
        '<div class="section-heading"><div><h2>报告中心</h2><p>日报、大盘、模型回测与稳健收益集中查看</p></div></div>'
        f'<div class="report-grid">{"".join(items)}</div></section>'
    )


def _account_cards(account_pages: list[AccountPage]) -> str:
    if not account_pages:
        return '<section class="panel"><h2>账户入口</h2><p class="muted">暂无持仓快照。</p></section>'

    cards = []
    for page in account_pages:
        href = _relative_href(page.output)
        counts = page.counts
        cards.append(
            f"""
<a class="card" href="{escape(href)}">
  <span class="card-title"><span>{escape(page.account)}</span><span class="card-arrow">→</span></span>
  <span class="account-counts">
    <span class="account-count"><strong>{counts.get('stock', 0)}</strong><span>A股</span></span>
    <span class="account-count"><strong>{counts.get('lof', 0)}</strong><span>LOF/ETF</span></span>
    <span class="account-count"><strong>{counts.get('otc', 0)}</strong><span>场外基金</span></span>
  </span>
</a>
"""
        )
    return (
        '<section class="dashboard-section">'
        '<div class="section-heading"><div><h2>账户入口</h2><p>按账户查看摘要、明细和基金组合复盘</p></div></div>'
        f'<div class="grid">{"".join(cards)}</div></section>'
    )


def _build_index(snapshot: dict, pages: list[ReportPage], account_pages: list[AccountPage]) -> str:
    generated_at = _now_text()
    source_url = str(snapshot.get("source_url", "") if isinstance(snapshot, dict) else "")
    source_line = SOURCE_TEXT
    if source_url:
        source_line = f'<a href="{escape(source_url)}">{SOURCE_TEXT}</a>'

    body = f"""
<header class="hero home-hero">
  <span class="hero-kicker">每日自动更新</span>
  <h1>每日持仓复盘</h1>
  <p class="hero-copy">查看持仓逐项分析、基金组合复盘、历史建议命中率，以及独立于当前持仓的沪深全市场稳健收益筛选。</p>
  <div class="meta-row">
    <span>生成时间：{escape(generated_at)}</span>
    <span>数据来源：{source_line}</span>
  </div>
</header>
{_reports_index_block(pages)}
{_account_cards(account_pages)}
<footer class="disclaimer">{escape(DISCLAIMER)}</footer>
"""
    return _wrap_html("每日持仓复盘", body)


def build_pages() -> list[Path]:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")

    snapshot = _load_holdings_snapshot()
    steady_income_data = _load_steady_income_data()
    report_pages = _build_report_pages(snapshot)
    account_pages = _build_account_pages(snapshot, report_pages)
    if report_pages and not account_pages:
        raise RuntimeError(
            "holding report pages exist but no account pages were generated; "
            "site_data/holdings_snapshot.json is missing or empty"
        )

    index_path = SITE_DIR / "index.html"
    steady_income_page_path = SITE_DIR / "steady_income.html"
    index_path.write_text(_build_index(snapshot, report_pages, account_pages), encoding="utf-8")
    steady_income_page_path.write_text(
        _build_steady_income_page(steady_income_data),
        encoding="utf-8",
    )
    public_data_dir = SITE_DIR / "data"
    public_data_dir.mkdir(parents=True, exist_ok=True)
    steady_income_public_data_path = public_data_dir / "steady_income.json"
    steady_income_public_data_path.write_text(
        json.dumps(steady_income_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    generated_files = [
        SITE_DIR / ".nojekyll",
        index_path,
        steady_income_page_path,
        steady_income_public_data_path,
    ]
    generated_files.extend(page.output for page in report_pages)
    generated_files.extend(page.output for page in account_pages)

    print(f"Built Pages report site: {SITE_DIR}")
    try:
        steady_label = steady_income_page_path.relative_to(ROOT_DIR)
    except ValueError:
        steady_label = steady_income_page_path
    print(f"Generated steady-income page: {steady_label}")
    if report_pages:
        print("Generated report pages:")
        for page in report_pages:
            print(f"  - {page.output.relative_to(ROOT_DIR)}")
    else:
        print("No report pages generated.")
    if account_pages:
        print("Generated account pages:")
        for page in account_pages:
            print(f"  - {page.output.relative_to(ROOT_DIR)}")
    else:
        print("No account pages generated.")
    return generated_files


def main() -> int:
    build_pages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
