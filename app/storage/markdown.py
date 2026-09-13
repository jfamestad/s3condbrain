"""Frontmatter parse/serialize (HANDOFF §3.4, §10.2).

Articles are stored as ``---\\n<yaml>\\n---\\n<body>``. Unknown frontmatter keys are
preserved on round-trip. ``seq`` is server-maintained and lives in frontmatter;
attribution (actor, kind) does **not** — it is object metadata (§8.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


def parse(raw: bytes | str) -> Article:
    """Split a stored object into frontmatter and body.

    A missing or malformed frontmatter block yields an empty mapping and the
    whole text as body — storage never refuses to read what it holds.
    """
    raise NotImplementedError


def serialize(frontmatter: dict[str, Any], body: str) -> bytes:
    """Render frontmatter + body as UTF-8 bytes. Keys are emitted in insertion
    order; ``type`` first, ``seq`` last, for readable diffs."""
    raise NotImplementedError


__all__ = ["Article", "parse", "serialize"]
