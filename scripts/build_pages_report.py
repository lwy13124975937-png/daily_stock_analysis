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
        return f"{date_text} 股票日报"
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


def _latest_report(pages: list[ReportPage], kind: str) -> ReportPage | None:
    return next((page for page in pages if page.kind == kind), None)


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


def _discover_reports() -> list[Path]:
    if not REPORTS_DIR.exists():
        print(f"No reports directory found: {REPORTS_DIR}")
        return []
    reports = sorted(REPORTS_DIR.glob("*.md"))
    if not reports:
        print(f"No Markdown reports found under: {REPORTS_DIR}")
    return reports


def _build_report_pages() -> list[ReportPage]:
    report_paths = _discover_reports()
    renderer = _load_markdown_renderer() if report_paths else None
    SITE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    pages: list[ReportPage] = []
    for report_path in report_paths:
        markdown_text = report_path.read_text(encoding="utf-8")
        title = _friendly_report_title(report_path, markdown_text)
        output_path = SITE_REPORTS_DIR / _html_name(report_path)
        html = renderer(markdown_text) if renderer else _wrap_html(title, escape(markdown_text))
        output_path.write_text(_enhance_report_html(html, title), encoding="utf-8")
        pages.append(
            ReportPage(
                source=report_path,
                output=output_path,
                title=title,
                kind=_report_kind(report_path),
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
    report_link = ""
    if latest_stock_report:
        href = f"../{_relative_href(latest_stock_report.output)}"
        report_link = f'<p><a href="{escape(href)}">查看股票日报</a></p>'

    sections = []
    for asset_type in TYPE_ORDER:
        label = TYPE_LABELS[asset_type]
        items = groups.get(asset_type, []) or []
        note = ""
        if asset_type == "lof" and items:
            note = '<p class="note">场内基金/LOF，分析仅作参考。</p>'
        if asset_type == "otc" and items:
            note = (
                '<p class="note">场外基金暂未接入股票日报分析。本页仅展示来自 '
                "stock-dashboard 的最新持仓清单，后续可接入基金净值、重仓行业和基金经理复盘。</p>"
            )
        sections.append(
            f"""
<details>
  <summary>{escape(label)}（{len(items)}）</summary>
  <div class="details-body">
    {note}
    {_account_items_table(items)}
  </div>
</details>
"""
        )

    body = f"""
<nav class="page-nav"><a href="../index.html">返回首页</a></nav>
<header class="hero">
  <h1>{escape(account)}持仓复盘</h1>
  <p class="muted">仅展示公开持仓字段。</p>
  {report_link}
</header>
{''.join(sections)}
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
            f'<li>股票日报：<a href="{escape(_relative_href(latest_stock.output))}">{escape(latest_stock.title)}</a></li>'
        )
    else:
        items.append('<li>股票日报：<span class="muted">暂无</span></li>')
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
{_account_cards(account_pages)}
{_reports_index_block(pages)}
<footer class="disclaimer">{escape(DISCLAIMER)}</footer>
"""
    return _wrap_html("每日持仓复盘", body)


def build_pages() -> list[Path]:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")

    report_pages = _build_report_pages()
    snapshot = _load_holdings_snapshot()
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
