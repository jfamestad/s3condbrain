"""Frontmatter parse/serialize (HANDOFF §3.4, §10.2).

Articles are stored as ``---\\n<yaml>\\n---\\n<body>``. Unknown frontmatter keys are
preserved on round-trip. ``seq`` is server-maintained and lives in frontmatter;
attribution (actor, kind) does **not** — it is object metadata (§8.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yaml

# A leading block: an opening ``---`` line, anything (non-greedy), then a closing
# ``---`` line. The closing line may end the text without a trailing newline.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)^---[ \t]*\r?(?:\n|\Z)",
    re.DOTALL | re.MULTILINE,
)


@dataclass
class Article:
    """Parsed article: frontmatter mapping plus markdown body (without the block)."""

    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def type(self) -> str:
        return str(self.frontmatter.get("type", "doc"))

    @property
    def seq(self) -> int:
        return int(self.frontmatter.get("seq", 0))


def _jsonable(value: Any) -> Any:
    """Coerce YAML-native scalars that JSON cannot carry (dates) to ISO strings.

    A hand-written ``at: 2026-01-01T00:00:00Z`` parses as a ``datetime``; the tool
    result is JSON, so it must go out as a string. Everything else passes through.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, date):  # datetime is a date subclass
        return value.isoformat()
    return value


def parse(raw: bytes | str) -> Article:
    """Split a stored object into frontmatter and body.

    A missing or malformed frontmatter block yields an empty mapping and the
    whole text as body — storage never refuses to read what it holds. A block
    that parses to something other than a mapping is treated as malformed.

    Args:
        raw: The stored object, as bytes (UTF-8, undecodable bytes replaced) or text.

    Returns:
        The parsed article. Date-like YAML scalars are coerced to ISO strings so
        the frontmatter is JSON-serialisable.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return Article(frontmatter={}, body=text)
    try:
        loaded = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError:
        return Article(frontmatter={}, body=text)
    if not isinstance(loaded, dict):
        return Article(frontmatter={}, body=text)
    return Article(frontmatter=_jsonable(loaded), body=text[match.end() :])


def serialize(frontmatter: dict[str, Any], body: str) -> bytes:
    """Render frontmatter + body as UTF-8 bytes. Keys are emitted in insertion
    order; ``type`` first, ``seq`` last, for readable diffs.

    Args:
        frontmatter: Mapping to emit. Unknown keys are kept verbatim.
        body: Markdown body without a frontmatter block.

    Returns:
        ``---\\n<yaml>---\\n<body>`` encoded as UTF-8.
    """
    ordered: dict[str, Any] = {}
    if "type" in frontmatter:
        ordered["type"] = frontmatter["type"]
    for key, value in frontmatter.items():
        if key not in ("type", "seq"):
            ordered[key] = value
    if "seq" in frontmatter:
        ordered["seq"] = frontmatter["seq"]
    block = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
    return f"---\n{block}---\n{body}".encode()


__all__ = ["Article", "parse", "serialize"]
