"""Shared HTML document layout for the static site."""

from __future__ import annotations

from html import escape


def document(
    title: str,
    body: str,
    *,
    build_id: str,
    asset_prefix: str = "",
    description: str = "每日持仓复盘",
) -> str:
    css_href = f"{asset_prefix}assets/site.css"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="build-id" content="{escape(build_id, quote=True)}">
  <meta name="description" content="{escape(description, quote=True)}">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="{escape(css_href, quote=True)}">
</head>
<body data-build-id="{escape(build_id, quote=True)}">
  <div class="shell">{body}</div>
</body>
</html>
"""


SITE_CSS = """
:root{color-scheme:light;--bg:#f4f7fb;--card:#fff;--text:#172033;--muted:#667085;--line:#d9e2ee;--accent:#075fca;--accent-soft:#eaf2ff;--good:#176c45;--warn:#8a5a00;--bad:#a73535;--shadow:0 8px 24px rgba(24,34,53,.055)}
*{box-sizing:border-box}html,body{min-width:0;margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65;overflow-x:hidden}body{overflow-wrap:anywhere}.shell{width:min(1180px,100%);margin:0 auto;padding:22px 18px 52px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}h1,h2,h3,h4,p{margin-top:0}h1{font-size:30px;line-height:1.25;margin-bottom:10px}h2{font-size:22px;margin-bottom:8px}h3{font-size:18px;margin-bottom:6px}.nav{display:flex;gap:14px;flex-wrap:wrap;padding:0 2px 14px;margin-bottom:16px;border-bottom:1px solid var(--line)}.hero,.panel,.card,.stock-row,.review,.steady-card,.metric-card{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:8px}.hero{padding:24px;margin-bottom:22px;box-shadow:var(--shadow)}.kicker{display:block;color:var(--accent);font-size:13px;font-weight:700;margin-bottom:6px}.lead,.muted,.meta{color:var(--muted)}.meta{display:flex;gap:8px 18px;flex-wrap:wrap;font-size:14px}.section{margin:24px 0}.section-head{display:flex;justify-content:space-between;align-items:end;gap:12px;margin-bottom:12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:12px}.card,.panel,.metric-card{padding:18px}.report-card{display:flex;flex-direction:column;gap:8px;min-height:145px}.report-card strong{font-size:19px}.action{margin-top:auto;font-weight:650}.counts,.metrics,.periods{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.count,.metric,.period{padding:10px;background:#f7f9fc;border:1px solid #e9eef5;border-radius:6px;text-align:center}.count strong,.metric strong{display:block;font-size:22px}.count span,.metric span,.period small{display:block;color:var(--muted);font-size:12px}.account-head,.stock-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.account-head{align-items:center;margin-bottom:12px}.account-summary{display:flex;gap:8px;flex-wrap:wrap}.pill,.badge{display:inline-flex;align-items:center;padding:3px 8px;border-radius:999px;font-size:12px;font-weight:650;background:var(--accent-soft);color:var(--accent)}.badge.good{background:#eaf7f0;color:var(--good)}.badge.warn{background:#fff5dc;color:var(--warn)}.badge.bad{background:#fdecec;color:var(--bad)}.holding-list,.compact-list{display:grid;gap:8px;padding:0;list-style:none}.holding-line{display:flex;justify-content:space-between;gap:12px;padding:10px 12px;background:#f8fafc;border:1px solid #e7edf4;border-radius:6px}.stock-row{margin-bottom:10px}.stock-row>summary,.collection>summary{cursor:pointer;list-style:none;padding:14px 16px;font-weight:700}.stock-row>summary::-webkit-details-marker,.collection>summary::-webkit-details-marker{display:none}.stock-row[open]>summary,.collection[open]>summary{border-bottom:1px solid var(--line)}.stock-head span:last-child{text-align:right;color:var(--muted);font-size:13px}.stock-body,.details-body{padding:16px}.conclusion{font-weight:620}.review{padding:18px;margin:14px 0}.review section{margin-top:14px}.review ul{margin:6px 0 0;padding-left:20px}.review li{margin:4px 0}.archive{margin-top:24px}.archive>summary{cursor:pointer;color:var(--muted);font-weight:650}.archive-body{margin-top:10px;padding:16px;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:auto}.archive-body table{display:block;max-width:100%;overflow:auto;border-collapse:collapse}.archive-body th,.archive-body td{border:1px solid var(--line);padding:6px 8px}.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:10px}.metric-card p{margin:4px 0}.record-list{display:grid;gap:8px}.record{padding:12px 14px;background:#fff;border:1px solid var(--line);border-radius:7px}.record-head{display:flex;justify-content:space-between;gap:10px}.periods{margin-top:8px}.period{text-align:left}.period strong,.period em{display:block;font-size:12px;font-style:normal}.steady-list{display:grid;gap:12px}.steady-card{padding:18px}.evidence-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:8px}.evidence{padding:10px;background:#f8fafc;border:1px solid #e8edf4;border-radius:6px}.evidence span{display:block;color:var(--muted);font-size:12px}.note{padding:14px;border:1px solid #e7bf53;background:#fff8df;border-radius:7px}.footer{margin-top:28px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}.compact-table{width:100%;border-collapse:collapse}.compact-table th,.compact-table td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}.status-line{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.raw-fragment img,.raw-fragment video,.raw-fragment iframe{display:none!important}
@media(max-width:680px){.shell{padding:14px 12px 36px}.hero,.panel,.card,.metric-card{padding:15px}h1{font-size:25px}h2{font-size:20px}.counts,.metrics,.periods{grid-template-columns:1fr}.stock-head,.record-head,.holding-line{align-items:flex-start;flex-direction:column}.stock-head span:last-child{text-align:left}.compact-table,.compact-table tbody,.compact-table tr,.compact-table td{display:block;width:100%}.compact-table thead{display:none}.compact-table tr{padding:8px 0;border-bottom:1px solid var(--line)}.compact-table td{padding:2px 0;border:0}.compact-table td::before{content:attr(data-label) "：";color:var(--muted)}}
""".strip() + "\n"
