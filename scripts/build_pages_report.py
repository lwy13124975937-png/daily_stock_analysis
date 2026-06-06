"""Build static HTML report pages from Markdown reports.

This script is intentionally separate from the analysis pipeline. It reads
``reports/*.md`` and writes a small static site under ``site/`` for GitHub Pages.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT_DIR / "reports"
SITE_DIR = ROOT_DIR / "site"
SITE_REPORTS_DIR = SITE_DIR / "reports"


@dataclass(frozen=True)
class ReportPage:
    source: Path
    output: Path
    title: str
    sort_key: tuple[str, float, str]


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


def _friendly_report_title(path: Path, markdown_text: str) -> str:
    date_text = _format_date(_extract_date_key(path))
    if re.fullmatch(r"market_review_20\d{6}", path.stem):
        return f"{date_text} 大盘复盘"
    if re.fullmatch(r"report_20\d{6}", path.stem):
        return f"{date_text} 股票日报"
    return _extract_title(markdown_text, path.stem)


def _html_name(path: Path) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip(".-")
    return f"{safe_stem or 'report'}.html"


def _enhance_report_html(html: str, title: str) -> str:
    """Add mobile viewport, Pages-friendly CSS, and a home link."""
    escaped_title = escape(title)
    generated_at = escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    additions = """
            html {
                -webkit-text-size-adjust: 100%;
            }
            body {
                box-sizing: border-box;
                width: min(100%, 960px);
                font-size: 16px;
                line-height: 1.75;
                overflow-wrap: anywhere;
            }
            h1 {
                font-size: 28px;
                line-height: 1.25;
                margin-top: 0;
            }
            h2 {
                font-size: 22px;
                line-height: 1.35;
            }
            h3 {
                font-size: 18px;
                line-height: 1.45;
            }
            table {
                display: block;
                overflow-x: auto;
                width: max-content;
                min-width: 100%;
                max-width: 100%;
                white-space: nowrap;
            }
            .page-nav {
                margin: 0 0 16px 0;
                padding-bottom: 12px;
                border-bottom: 1px solid #dfe2e5;
            }
            .page-nav a {
                color: #0969da;
                font-weight: 600;
                text-decoration: none;
            }
            .report-header {
                margin-bottom: 20px;
                padding-bottom: 16px;
                border-bottom: 1px solid #d8dee4;
            }
            .report-header h1 {
                margin-bottom: 8px;
            }
            .muted {
                color: #6e7781;
            }
            .disclaimer {
                margin-top: 32px;
                padding-top: 16px;
                border-top: 1px solid #d8dee4;
                color: #6e7781;
                font-size: 14px;
            }
            @media (max-width: 640px) {
                body {
                    padding: 16px;
                    font-size: 15px;
                }
                h1 {
                    font-size: 24px;
                }
                h2 {
                    font-size: 20px;
                }
                th, td {
                    padding: 6px 8px;
                }
            }
        """
    header = f"""
            <nav class="page-nav"><a href="../index.html">返回首页</a></nav>
            <header class="report-header">
                <h1>{escaped_title}</h1>
                <p class="muted">生成时间：{generated_at}</p>
            </header>
            """
    footer = """
            <footer class="disclaimer">本页面内容由 AI 自动生成，仅作复盘参考，不构成投资建议。</footer>
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
        html = html.replace("<head>", f"<head>\n            <title>{escaped_title}</title>", 1)
    if "</style>" in html:
        html = html.replace("</style>", additions + "\n            </style>", 1)
    if "<body>" in html:
        html = re.sub(r"<h1\b[^>]*>.*?</h1>\s*", "", html, count=1, flags=re.IGNORECASE | re.DOTALL)
        html = html.replace("<body>", f"<body>\n            {header}", 1)
    if "</body>" in html:
        html = html.replace("</body>", f"{footer}\n        </body>", 1)
    return html


def _wrap_html(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{
      box-sizing: border-box;
      width: min(100%, 960px);
      margin: 0 auto;
      padding: 16px;
      color: #24292f;
      font: 16px/1.75 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      overflow-wrap: anywhere;
    }}
    a {{ color: #0969da; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    h1 {{ font-size: 30px; line-height: 1.25; margin: 0 0 12px; }}
    h2 {{ font-size: 22px; line-height: 1.35; margin-top: 28px; }}
    .muted {{ color: #6e7781; }}
    .hero {{ border-bottom: 1px solid #d8dee4; margin-bottom: 22px; padding-bottom: 16px; }}
    .latest-report {{
      border: 1px solid #d8dee4;
      border-radius: 8px;
      margin: 18px 0 26px;
      padding: 16px;
      background: #f6f8fa;
    }}
    .latest-report a {{ font-size: 18px; font-weight: 700; }}
    .report-list {{ list-style: none; padding: 0; margin: 18px 0; }}
    .report-list li {{ border-bottom: 1px solid #d8dee4; padding: 12px 0; }}
    .report-list a {{ font-weight: 600; }}
    .disclaimer {{
      margin-top: 32px;
      padding-top: 16px;
      border-top: 1px solid #d8dee4;
      color: #6e7781;
      font-size: 14px;
    }}
    table {{
      display: block;
      width: max-content;
      min-width: 100%;
      max-width: 100%;
      overflow-x: auto;
      border-collapse: collapse;
      white-space: nowrap;
    }}
    th, td {{ border: 1px solid #d8dee4; padding: 6px 8px; }}
    th {{ background: #f6f8fa; }}
    @media (max-width: 640px) {{
      body {{ padding: 16px; font-size: 15px; }}
      h1 {{ font-size: 25px; }}
      h2 {{ font-size: 20px; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _build_index(pages: list[ReportPage]) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not pages:
        body = f"""
<header class="hero">
  <h1>每日股票分析报告</h1>
  <p class="muted">最新生成时间：{escape(generated_at)}</p>
</header>
<p>暂未在 <code>reports/</code> 目录下发现 Markdown 报告。</p>
<footer class="disclaimer">本页面内容由 AI 自动生成，仅作复盘参考，不构成投资建议。</footer>
"""
        return _wrap_html("每日股票分析报告", body)

    items = []
    for page in pages[1:]:
        href = f"reports/{escape(page.output.name)}"
        title = escape(page.title)
        items.append(f'<li><a href="{href}">{title}</a></li>')

    latest = pages[0]
    latest_href = f"reports/{escape(latest.output.name)}"
    latest_title = escape(latest.title)
    history = chr(10).join(items) if items else '<li><span class="muted">暂无更多历史报告</span></li>'

    body = f"""
<header class="hero">
  <h1>每日股票分析报告</h1>
  <p class="muted">最新生成时间：{escape(generated_at)}</p>
</header>
<section class="latest-report">
  <h2>最新报告入口</h2>
  <p><a href="{latest_href}">{latest_title}</a></p>
</section>
<section>
<h2>历史报告列表</h2>
<ul class="report-list">
{history}
</ul>
</section>
<footer class="disclaimer">本页面内容由 AI 自动生成，仅作复盘参考，不构成投资建议。</footer>
"""
    return _wrap_html("每日股票分析报告", body)


def _discover_reports() -> list[Path]:
    if not REPORTS_DIR.exists():
        print(f"No reports directory found: {REPORTS_DIR}")
        return []
    reports = sorted(REPORTS_DIR.glob("*.md"))
    if not reports:
        print(f"No Markdown reports found under: {REPORTS_DIR}")
    return reports


def build_pages() -> list[Path]:
    report_paths = _discover_reports()
    renderer = _load_markdown_renderer() if report_paths else None
    SITE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")

    pages: list[ReportPage] = []
    generated_files = [SITE_DIR / ".nojekyll"]

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
                sort_key=(
                    _extract_date_key(report_path),
                    report_path.stat().st_mtime,
                    report_path.name,
                ),
            )
        )
        generated_files.append(output_path)

    pages.sort(key=lambda page: page.sort_key, reverse=True)
    index_path = SITE_DIR / "index.html"
    index_path.write_text(_build_index(pages), encoding="utf-8")
    generated_files.append(index_path)

    print(f"Built Pages report site: {SITE_DIR}")
    if pages:
        print("Generated report pages:")
        for page in pages:
            print(f"  - {page.output.relative_to(ROOT_DIR)}")
    else:
        print("Generated empty report index.")
    return generated_files


def main() -> int:
    build_pages()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
