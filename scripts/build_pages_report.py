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


def _extract_date_key(path: Path) -> str:
    match = re.search(r"(20\d{6})", path.stem)
    if match:
        return match.group(1)
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")


def _html_name(path: Path) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip(".-")
    return f"{safe_stem or 'report'}.html"


def _enhance_report_html(html: str, title: str) -> str:
    """Add mobile viewport, Pages-friendly CSS, and a home link."""
    escaped_title = escape(title)
    additions = """
            html {
                -webkit-text-size-adjust: 100%;
            }
            body {
                box-sizing: border-box;
                width: min(100%, 960px);
                font-size: 16px;
                line-height: 1.65;
                overflow-wrap: anywhere;
            }
            table {
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
            @media (max-width: 640px) {
                body {
                    padding: 12px;
                    font-size: 15px;
                }
                h1 {
                    font-size: 21px;
                }
                h2 {
                    font-size: 18px;
                }
                th, td {
                    padding: 6px 8px;
                }
            }
        """
    nav = f'<nav class="page-nav"><a href="../index.html">返回首页</a></nav>'

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
        html = html.replace("<body>", f"<body>\n            {nav}", 1)
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
      font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      overflow-wrap: anywhere;
    }}
    a {{ color: #0969da; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .muted {{ color: #6e7781; }}
    .report-list {{ list-style: none; padding: 0; margin: 18px 0; }}
    .report-list li {{ border-bottom: 1px solid #d8dee4; padding: 12px 0; }}
    .report-list a {{ font-weight: 600; }}
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
      body {{ padding: 12px; font-size: 15px; }}
      h1 {{ font-size: 22px; }}
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
<h1>Daily Stock Analysis Reports</h1>
<p class="muted">Generated at {escape(generated_at)}</p>
<p>No Markdown reports were found under <code>reports/</code>.</p>
"""
        return _wrap_html("Daily Stock Analysis Reports", body)

    items = []
    for page in pages:
        href = f"reports/{escape(page.output.name)}"
        source_name = escape(page.source.name)
        title = escape(page.title)
        items.append(
            f'<li><a href="{href}">{title}</a><br><span class="muted">{source_name}</span></li>'
        )

    body = f"""
<h1>Daily Stock Analysis Reports</h1>
<p class="muted">Generated at {escape(generated_at)}</p>
<ul class="report-list">
{chr(10).join(items)}
</ul>
"""
    return _wrap_html("Daily Stock Analysis Reports", body)


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
        title = _extract_title(markdown_text, report_path.stem)
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
