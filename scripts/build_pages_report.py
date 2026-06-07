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


ROOT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT_DIR / "reports"
SITE_DATA_DIR = ROOT_DIR / "site_data"
HOLDINGS_SNAPSHOT_PATH = SITE_DATA_DIR / "holdings_snapshot.json"
SITE_DIR = ROOT_DIR / "site"
SITE_REPORTS_DIR = SITE_DIR / "reports"
SITE_ACCOUNTS_DIR = SITE_DIR / "accounts"

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
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_holdings_snapshot() -> dict:
    if not HOLDINGS_SNAPSHOT_PATH.exists():
        print(f"No holdings snapshot found: {HOLDINGS_SNAPSHOT_PATH}")
        return {}
    try:
        return json.loads(HOLDINGS_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read holdings snapshot: {type(exc).__name__}: {exc}")
        return {}


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
        name = str(item.get("name", "") or "").strip()
        code = str(item.get("code", "") or "").strip()
        if name and code:
            pairs.append((name, code))
    return pairs


def _append_ai_snippet(
    by_code: dict[str, list[str]],
    code: str,
    snippet: str,
    *,
    max_chars: int | None = MAX_AI_SNIPPET_CHARS,
) -> None:
    text = snippet.strip()
    if not text:
        return
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    if re.fullmatch(r"[\s|:：\-]+", text):
        return
    existing = by_code.setdefault(code, [])
    if text in existing or len(existing) >= MAX_AI_SNIPPETS_PER_CODE:
        return
    existing.append(text)


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
            if next_heading.level <= heading.level:
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
            codes = _codes_in_text(item)
            if len(codes) != 1:
                return
            code = codes[0]
            _append_ai_snippet(by_code, code, item, max_chars=MAX_AI_SNIPPET_CHARS)

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
    rendered = ['<p class="muted">本次分析未完成，以下为失败原因：</p>']
    for failure in failures[:MAX_AI_SNIPPETS_PER_CODE]:
        rendered.append(f'<pre class="ai-snippet">{escape(_summary_detail_text(failure, code))}</pre>')
    return "".join(rendered)


def _sanitize_ai_snippet_for_holding(snippet: str, code: str) -> str:
    """Keep analysis text, but remove AI-provided display names adjacent to code."""
    escaped_code = re.escape(code)
    text = snippet
    text = re.sub(
        rf"[\u4e00-\u9fffA-Za-z0-9*·_\-/\s]{{1,40}}[（(]\s*{escaped_code}\s*[）)]",
        code,
        text,
    )
    text = re.sub(
        rf"{escaped_code}\s*[-—:：]\s*[^\n|，。；;]{{1,40}}",
        code,
        text,
    )
    return text.strip()


def _render_text_snippets(snippets: list[str], code: str) -> str:
    if not snippets:
        return '<p class="muted">AI 暂未输出该标的分析，仅展示持仓清单。</p>'
    rendered = []
    for snippet in snippets:
        safe_text = escape(_sanitize_ai_snippet_for_holding(snippet, code))
        rendered.append(f'<pre class="ai-snippet">{safe_text}</pre>')
    return "".join(rendered)


def _summary_detail_text(summary: str, code: str) -> str:
    escaped_code = re.escape(code)
    text = summary.strip()
    text = re.sub(r"^[-*+]\s+", "", text)
    text = re.sub(r"^\d+[.)、]\s+", "", text)
    text = re.sub(rf"^.*?[（(]\s*{escaped_code}\s*[）)]\s*[:：]\s*", "", text)
    text = re.sub(rf"^{escaped_code}\s*[:：]\s*", "", text)
    return text.strip() or "有 AI 摘要项，详见原始股票日报。"


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
      --bg: #f6f8fb;
      --card: #ffffff;
      --text: #1f2328;
      --muted: #656d76;
      --line: #d8dee4;
      --link: #0969da;
      --soft: #f6f8fa;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      width: min(100%, 960px);
      margin: 0 auto;
      padding: 18px 16px 32px;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.75 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      overflow-wrap: anywhere;
    }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    h1 {{ font-size: 30px; line-height: 1.25; margin: 0 0 10px; }}
    h2 {{ font-size: 22px; line-height: 1.35; margin: 28px 0 12px; }}
    h3 {{ font-size: 18px; line-height: 1.45; margin: 18px 0 8px; }}
    .muted {{ color: var(--muted); }}
    .hero {{
      margin-bottom: 18px;
      padding: 18px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .card {{
      display: block;
      min-width: 0;
      padding: 16px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .card:hover {{ text-decoration: none; border-color: #8c959f; }}
    .card-title {{ display: block; font-size: 18px; font-weight: 700; margin-bottom: 10px; }}
    .count-list {{ list-style: none; padding: 0; margin: 0; color: var(--muted); }}
    .count-list li {{ margin: 4px 0; }}
    .link-list {{ list-style: none; padding: 0; margin: 0; }}
    .link-list li {{
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }}
    .panel {{
      margin-top: 14px;
      padding: 16px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .page-nav {{
      margin-bottom: 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line);
    }}
    details {{
      margin: 12px 0;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    summary {{
      cursor: pointer;
      padding: 13px 14px;
      font-weight: 700;
      background: var(--soft);
    }}
    .details-body {{ padding: 12px 14px 14px; }}
    .holding-table-wrap {{ width: 100%; overflow-x: auto; }}
    table {{
      display: block;
      width: max-content;
      min-width: 100%;
      max-width: 100%;
      overflow-x: auto;
      border-collapse: collapse;
      white-space: nowrap;
      background: var(--card);
    }}
    th, td {{ border: 1px solid var(--line); padding: 8px 10px; text-align: left; }}
    th {{ background: var(--soft); }}
    .note {{
      margin: 12px 0;
      padding: 12px;
      background: #fff8c5;
      border: 1px solid #eac54f;
      border-radius: 8px;
    }}
    .holding-item {{
      margin: 12px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--card);
    }}
    .holding-item h4 {{
      margin: 0 0 6px;
      font-size: 16px;
    }}
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
    .disclaimer {{
      margin-top: 30px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 14px;
    }}
    @media (max-width: 720px) {{
      body {{ padding: 16px; font-size: 15px; }}
      h1 {{ font-size: 25px; }}
      h2 {{ font-size: 20px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .hero, .card, .panel {{ padding: 14px; }}
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
                <h1>{escape(title)}</h1>
                <p class="muted">生成时间：{escape(_now_text())}</p>
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
            table { display: block; overflow-x: auto; white-space: nowrap; }
            body { width: min(100%, 960px); line-height: 1.75; }
            @media (max-width: 720px) { body { padding: 16px; } }
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
<details>
  <summary>原始 AI 股票日报</summary>
  <div class="details-body raw-report">
    {raw_report_html}
  </div>
</details>
"""


def _render_unmatched_ai_details(unmatched: dict[str, list[str]]) -> str:
    if not unmatched:
        return """
<details>
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
<details>
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
        snippets.append(f'<pre class="ai-snippet">{escape(detail)}</pre>')
    return (
        '<p class="muted">暂无完整单项分析，以下为 AI 摘要：</p>'
        + "".join(snippets)
    )


def _holding_analysis_card(
    item: dict,
    snippets_by_code: dict[str, list[str]],
    summary_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
) -> str:
    name = str(item.get("name", "") or "-")
    code = str(item.get("code", "") or "-")
    account = str(item.get("account", "") or "")
    asset_type = str(item.get("type", "") or "")
    label = TYPE_LABELS.get(asset_type, asset_type or "-")
    if asset_type == "otc":
        analysis = (
            '<p class="note">场外基金暂未接入股票日报分析。本页仅展示来自 '
            "stock-dashboard 的最新持仓清单，后续可接入基金净值、重仓行业和基金经理复盘。</p>"
        )
    elif snippets_by_code.get(code):
        analysis = _render_text_snippets(snippets_by_code.get(code, []), code)
    elif unfinished_by_code.get(code):
        analysis = _render_failure_snippets(unfinished_by_code.get(code, []), code)
    else:
        analysis = _render_summary_fallback(summary_by_code, code)
    return f"""
<article class="holding-item" data-account="{escape(account, quote=True)}" data-code="{escape(code, quote=True)}" data-type="{escape(asset_type, quote=True)}">
  <h4>{escape(name)}</h4>
  <p class="muted">代码：{escape(code)} ｜ 类型：{escape(label)}</p>
  <div>{analysis}</div>
</article>
"""


def _render_holding_cards(
    items: list[dict],
    snippets_by_code: dict[str, list[str]],
    summary_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
) -> str:
    if not items:
        return '<p class="muted">暂无持仓。</p>'
    return "".join(
        _holding_analysis_card(item, snippets_by_code, summary_by_code, unfinished_by_code)
        for item in items
    )


def _render_account_summary(
    account: str,
    items: list[dict],
    summary_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
) -> str:
    rows = []
    for item in items:
        code = str(item.get("code", "") or "").strip()
        name = str(item.get("name", "") or "-")
        asset_type = str(item.get("type", "") or "")
        label = TYPE_LABELS.get(asset_type, asset_type or "-")
        summaries = summary_by_code.get(code, [])
        if asset_type == "otc":
            detail = "场外基金暂未接入股票日报分析，仅展示持仓清单。"
        elif summaries:
            detail = "；".join(
                _summary_detail_text(summary, code)
                for summary in summaries[:MAX_AI_SNIPPETS_PER_CODE]
            )
        elif unfinished_by_code.get(code):
            detail = "分析失败：" + "；".join(
                _summary_detail_text(failure, code)
                for failure in unfinished_by_code.get(code, [])[:MAX_AI_SNIPPETS_PER_CODE]
            )
        else:
            detail = "AI摘要缺失"
        rows.append(
            f'<li class="summary-item" data-account="{escape(account, quote=True)}" '
            f'data-code="{escape(code, quote=True)}" data-type="{escape(asset_type, quote=True)}">'
            f"<strong>{escape(name)}（{escape(code)}）</strong>"
            f" <span class=\"muted\">{escape(label)}</span>：{escape(detail)}"
            "</li>"
        )
    body = f'<ul class="link-list">{"".join(rows)}</ul>' if rows else '<p class="muted">暂无持仓。</p>'
    return f"""
<section class="panel">
  <h3>{escape(account)}分析结果摘要</h3>
  {body}
</section>
"""


def _render_standard_account_section(
    account: str,
    groups: dict,
    summary_by_code: dict[str, list[str]],
    snippets_by_code: dict[str, list[str]],
    unfinished_by_code: dict[str, list[str]],
    is_open: bool,
) -> str:
    items = _account_items(groups)
    return f"""
<details {"open" if is_open else ""}>
  <summary>{escape(account)}</summary>
  <div class="details-body">
    {_render_account_summary(account, items, summary_by_code, unfinished_by_code)}
    <section class="panel">
      <h3>{escape(account)}持仓明细与分析</h3>
      {_render_holding_cards(items, snippets_by_code, summary_by_code, unfinished_by_code)}
    </section>
  </div>
</details>
"""


def _build_holding_report_page(
    title: str,
    markdown_text: str,
    raw_report_html: str,
    snapshot: dict,
) -> str:
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
    snapshot_codes = _all_snapshot_codes(snapshot)
    unmatched: dict[str, list[str]] = {}
    for source in (summary_by_code, snippets_by_code, unfinished_by_code):
        for code, snippets in source.items():
            if code in snapshot_codes:
                continue
            for snippet in snippets:
                _append_ai_snippet(unmatched, code, snippet)

    sections = []
    for idx, account in enumerate(account_names):
        groups = accounts.get(account, {}) if isinstance(accounts, dict) else {}
        if not isinstance(groups, dict):
            groups = {}
        sections.append(
            _render_standard_account_section(
                account,
                groups,
                summary_by_code,
                snippets_by_code,
                unfinished_by_code,
                idx == 0,
            )
        )

    body = f"""
<nav class="page-nav"><a href="../index.html">返回首页</a></nav>
<header class="hero">
  <h1>{escape(title)}</h1>
  <p class="muted">生成时间：{escape(_now_text())}</p>
  <p class="muted">本页根据 stock-dashboard 最新 holdings_data.json 生成。持仓名称、代码、账户和类型以 holdings_data.json 为准。AI 分析仅作复盘参考，不构成投资建议。</p>
</header>
{''.join(sections)}
{_render_raw_report_details(raw_report_html)}
{_render_unmatched_ai_details(unmatched)}
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
        raw_html = renderer(markdown_text) if renderer else _wrap_html(title, escape(markdown_text))
        if kind == "stock":
            html = _build_holding_report_page(title, markdown_text, raw_html, snapshot)
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


def _account_items_table(items: list[dict]) -> str:
    if not items:
        return '<p class="muted">暂无该类型持仓。</p>'
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('name', '') or '-'))}</td>"
            f"<td>{escape(str(item.get('code', '') or '-'))}</td>"
            f"<td>{escape(TYPE_LABELS.get(str(item.get('type', '')), str(item.get('type', ''))))}</td>"
            "</tr>"
        )
    return (
        '<div class="holding-table-wrap"><table>'
        "<thead><tr><th>名称</th><th>代码</th><th>类型</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _build_account_page(account: str, groups: dict, latest_stock_report: ReportPage | None) -> str:
    report_link = '<p class="muted">本次构建未发现 report_*.md，最新持仓日报暂不可用。</p>'
    if latest_stock_report:
        href = f"../{_relative_href(latest_stock_report.output)}"
        report_link = f'<p><a href="{escape(href)}">查看最新持仓日报</a></p>'

    items = _account_items(groups)
    notes = []
    if any(str(item.get("type", "")) == "lof" for item in items):
        notes.append('<p class="note">场内基金/ETF/LOF 已纳入持仓日报分析，结论仅作复盘参考。</p>')
    if any(str(item.get("type", "")) == "otc" for item in items):
        notes.append(
            '<p class="note">场外基金暂未接入股票日报分析。本页仅展示来自 '
            "stock-dashboard 的最新持仓清单，后续可接入基金净值、重仓行业和基金经理复盘。</p>"
        )

    body = f"""
<nav class="page-nav"><a href="../index.html">返回首页</a></nav>
<header class="hero">
  <h1>{escape(account)}持仓复盘</h1>
  <p class="muted">仅展示公开持仓字段。完整 AI 分析请查看最新持仓日报。</p>
  {report_link}
</header>
<section class="panel">
  <h2>{escape(account)}持仓清单</h2>
  {''.join(notes)}
  {_account_items_table(items)}
</section>
<footer class="disclaimer">{escape(DISCLAIMER)}</footer>
"""
    return _wrap_html(f"{account}持仓复盘", body)


def _build_account_pages(snapshot: dict, pages: list[ReportPage]) -> list[AccountPage]:
    SITE_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    accounts = snapshot.get("accounts", {}) if isinstance(snapshot, dict) else {}
    latest_stock_report = _latest_report(pages, "stock")
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
            _build_account_page(str(account), groups, latest_stock_report),
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


def _reports_index_block(pages: list[ReportPage]) -> str:
    latest_stock = _latest_report(pages, "stock")
    latest_market = _latest_report(pages, "market")
    items = []
    if latest_stock:
        items.append(
            f'<li>最新持仓日报：<a href="{escape(_relative_href(latest_stock.output))}">{escape(latest_stock.title)}</a></li>'
        )
    else:
        items.append('<li>最新持仓日报：<span class="muted">暂无</span></li>')
    if latest_market:
        items.append(
            f'<li>大盘复盘：<a href="{escape(_relative_href(latest_market.output))}">{escape(latest_market.title)}</a></li>'
        )
    else:
        items.append('<li>大盘复盘：<span class="muted">暂无</span></li>')
    return f'<section class="panel"><h2>最新报告入口</h2><ul class="link-list">{"".join(items)}</ul></section>'


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
  <span class="card-title">{escape(page.account)}</span>
  <ul class="count-list">
    <li>股票数量：{counts.get('stock', 0)}</li>
    <li>LOF/ETF数量：{counts.get('lof', 0)}</li>
    <li>场外基金数量：{counts.get('otc', 0)}</li>
  </ul>
</a>
"""
        )
    return f'<section><h2>账户入口</h2><div class="grid">{"".join(cards)}</div></section>'


def _build_index(snapshot: dict, pages: list[ReportPage], account_pages: list[AccountPage]) -> str:
    generated_at = snapshot.get("generated_at") if isinstance(snapshot, dict) else ""
    generated_at = str(generated_at or _now_text())
    source_url = str(snapshot.get("source_url", "") if isinstance(snapshot, dict) else "")
    source_line = SOURCE_TEXT
    if source_url:
        source_line = f'<a href="{escape(source_url)}">{SOURCE_TEXT}</a>'

    body = f"""
<header class="hero">
  <h1>每日持仓复盘</h1>
  <p class="muted">生成时间：{escape(generated_at)}</p>
  <p class="muted">数据来源：{source_line}</p>
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
    report_pages = _build_report_pages(snapshot)
    account_pages = _build_account_pages(snapshot, report_pages)

    index_path = SITE_DIR / "index.html"
    index_path.write_text(_build_index(snapshot, report_pages, account_pages), encoding="utf-8")

    generated_files = [SITE_DIR / ".nojekyll", index_path]
    generated_files.extend(page.output for page in report_pages)
    generated_files.extend(page.output for page in account_pages)

    print(f"Built Pages report site: {SITE_DIR}")
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
