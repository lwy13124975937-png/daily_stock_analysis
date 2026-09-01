"""Public HTML and URL security primitives.

All HTML originating from Markdown, an LLM, or an external provider must pass
through this module before it is inserted into a public page.
"""

from __future__ import annotations

from html import escape
from urllib.parse import urlparse

import nh3


SAFE_URL_SCHEMES = {"http", "https", "mailto"}
SAFE_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "details",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "summary",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
SAFE_ATTRIBUTES = {
    "a": {"href", "title"},
    "details": {"open"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}


def safe_public_url(value: object, *, allow_mailto: bool = False) -> str | None:
    """Return a normalized public URL or ``None`` for unsafe/relative input."""

    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    allowed = {"https"}
    if allow_mailto:
        allowed.add("mailto")
    if parsed.scheme.lower() not in allowed:
        return None
    if parsed.scheme.lower() == "https" and not parsed.netloc:
        return None
    if any(char in candidate for char in ("\x00", "\r", "\n")):
        return None
    return candidate


def sanitize_html(fragment: object) -> str:
    """Sanitize an untrusted HTML fragment with an explicit allowlist."""

    return nh3.clean(
        str(fragment or ""),
        tags=SAFE_TAGS,
        attributes=SAFE_ATTRIBUTES,
        url_schemes=SAFE_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def safe_link(url: object, label: object) -> str:
    target = safe_public_url(url)
    if target is None:
        return escape(str(label or ""))
    return (
        f'<a href="{escape(target, quote=True)}" rel="noopener noreferrer nofollow">'
        f"{escape(str(label or ''))}</a>"
    )
