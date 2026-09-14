"""Markdown → HTML for the read view (HANDOFF §11.6).

markdown-it-py with HTML disabled: article bodies are agent-written and therefore
untrusted (§4.7, §7); raw HTML in them is never rendered. Internal links of the
form ``/a/<path>`` (§7 grammar) and bare ``/<path>.md`` are rewritten to the web
app's article route so the tree is browsable; every other link gets
``rel="noopener noreferrer"``.
"""

from __future__ import annotations

import re
from html import escape

from markdown_it import MarkdownIt
from markdown_it.token import Token

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_md.enable("table")

_INTERNAL = re.compile(r"^/a(/[^\s?#]+\.md)$|^(/[^\s?#:]+\.md)$")


def _rewrite_links(tokens: list[Token], app_prefix: str) -> None:
    for tok in tokens:
        if tok.type == "link_open":
            href = str(tok.attrGet("href") or "")
            m = _INTERNAL.match(href)
            if m:
                path = m.group(1) or m.group(2)
                tok.attrSet("href", f"{app_prefix}/a{path}")
            else:
                tok.attrSet("rel", "noopener noreferrer")
        if tok.children:
            _rewrite_links(tok.children, app_prefix)


def render(markdown: str, app_prefix: str = "/app") -> str:
    """Render untrusted markdown to safe HTML."""
    tokens = _md.parse(markdown)
    _rewrite_links(tokens, app_prefix)
    return _md.renderer.render(tokens, _md.options, {})


def sections(markdown: str) -> list[tuple[int, str]]:
    """``(level, heading text)`` for every ATX heading — the in-page table of contents."""
    out: list[tuple[int, str]] = []
    for line in markdown.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            out.append((len(m.group(1)), escape(m.group(2))))
    return out


__all__ = ["render", "sections"]
