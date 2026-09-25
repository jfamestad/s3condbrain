"""``edit_article`` — ``wiki.write`` · ``write`` (patch edits over ``update_article``).

Same sequence as ``update_article``: validate → grant check on the path → mint a
WRITE credential for the exact key → ``GetObject`` → compare ``if_version`` →
``PutObject`` with ``If-Match`` → refresh the parent listing. The only difference is
where the new article comes from: the body is the current body with ``edits``
applied in order, and the frontmatter is the stored block with ``frontmatter``
merged in at the top level (a ``null`` value removes the key). The write tail is
``update_article.write_current``, shared rather than copied.

Every edit is applied in memory before anything is written, so a failing edit
writes nothing. A stale ``if_version`` is a lean 409: ``current_version`` plus
``edits_apply`` — whether the same edits would succeed on the current body — and
no ``current_body``, which is the whole point of not resending it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import MAX_ARTICLE_BYTES, SCOPE_WRITE
from app.errors import ToolError, bad_request, conflict
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_PATH_SCHEMA,
    SECTION_MAX_LENGTH,
    VERSION_SCHEMA,
    WRITE_RESULT_SCHEMA,
    article_path,
    require_grant,
    revalidate_stored,
    store,
    validate_frontmatter,
    version_arg,
)
from app.mcp.tools.read_article import section_span
from app.mcp.tools.update_article import live_for_update, write_current
from app.storage.articles import StoredObject
from app.storage.markdown import parse

DESCRIPTION = (
    "Edit part of an article — replace exact text, append to the end or to a section, "
    "or change individual frontmatter fields — without resending the whole body. "
    "Prefer this over update_article for small changes. Put every change to one "
    "article in a single call (edits apply in order); edit different articles with "
    "parallel calls."
)

MAX_EDITS = 50

STALE_MESSAGE = (
    "Changed since you read it. If edits_apply is true, retry with if_version = "
    "current_version; otherwise read the article and redo the edits."
)

_REPLACE_KEYS = frozenset({"old", "new", "replace_all"})
_APPEND_KEYS = frozenset({"append", "section"})

_TEXT_SCHEMA: dict[str, Any] = {"type": "string", "maxLength": MAX_ARTICLE_BYTES}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "if_version"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "if_version": VERSION_SCHEMA,
        "edits": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_EDITS,
            "description": (
                "Applied in order, each to the result of the ones before. If any edit "
                "fails, nothing is written."
            ),
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["old", "new"],
                        "additionalProperties": False,
                        "properties": {
                            "old": {
                                **_TEXT_SCHEMA,
                                "minLength": 1,
                                "description": (
                                    "Exact text to replace. Must match once unless "
                                    "replace_all is set."
                                ),
                            },
                            "new": _TEXT_SCHEMA,
                            "replace_all": {"type": "boolean", "default": False},
                        },
                    },
                    {
                        "type": "object",
                        "required": ["append"],
                        "additionalProperties": False,
                        "properties": {
                            "append": {
                                **_TEXT_SCHEMA,
                                "minLength": 1,
                                "description": (
                                    "Text to add at the end of the body, or at the end "
                                    "of 'section' when given."
                                ),
                            },
                            "section": {
                                "type": "string",
                                "maxLength": SECTION_MAX_LENGTH,
                                "description": (
                                    "Markdown heading text, matched case-insensitively. "
                                    "The text goes at the end of that section, before "
                                    "the next heading of the same or higher level."
                                ),
                            },
                        },
                    },
                ]
            },
        },
        "frontmatter": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Fields merged into the stored frontmatter at the top level; a field "
                "set to null is removed. 'seq' is server-maintained and ignored."
            ),
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    **WRITE_RESULT_SCHEMA,
    "required": [*WRITE_RESULT_SCHEMA["required"], "total_bytes"],
    "properties": {
        **WRITE_RESULT_SCHEMA["properties"],
        "total_bytes": {"type": "integer", "description": "Byte length of the new body."},
    },
}


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _text(edit: dict[str, Any], key: str, where: str, *, non_empty: bool) -> str:
    value = edit.get(key)
    if not isinstance(value, str) or (non_empty and not value):
        kind = "a non-empty string" if non_empty else "a string"
        raise bad_request(f"'{where}.{key}' is required and must be {kind}.")
    if len(value.encode("utf-8")) > MAX_ARTICLE_BYTES:
        raise bad_request(f"'{where}.{key}' exceeds the {MAX_ARTICLE_BYTES} byte article limit.")
    return value


def _edit_arg(edit: Any, index: int) -> dict[str, Any]:
    where = f"edits[{index}]"
    if not isinstance(edit, dict):
        raise bad_request(f"'{where}' must be an object.")
    keys = set(edit)
    if "old" in keys and keys <= _REPLACE_KEYS:
        replace_all = edit.get("replace_all", False)
        if not isinstance(replace_all, bool):
            raise bad_request(f"'{where}.replace_all' must be a boolean.")
        return {
            "old": _text(edit, "old", where, non_empty=True),
            "new": _text(edit, "new", where, non_empty=False),
            "replace_all": replace_all,
        }
    if "append" in keys and keys <= _APPEND_KEYS:
        section = edit.get("section")
        if section is not None and (
            not isinstance(section, str) or not section.strip() or len(section) > SECTION_MAX_LENGTH
        ):
            raise bad_request(f"'{where}.section' must be a non-empty heading text.")
        return {"append": _text(edit, "append", where, non_empty=True), "section": section}
    raise bad_request(
        f"'{where}' must be exactly one of {{old, new, replace_all?}} or {{append, section?}}."
    )


def _edits_arg(args: dict[str, Any]) -> list[dict[str, Any]]:
    value = args.get("edits")
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_EDITS:
        raise bad_request(f"'edits' must be an array of 1 to {MAX_EDITS} edits.")
    return [_edit_arg(edit, index) for index, edit in enumerate(value)]


def _frontmatter_arg(args: dict[str, Any]) -> dict[str, Any] | None:
    value = args.get("frontmatter")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise bad_request("'frontmatter' must be an object of fields to merge.")
    return {k: v for k, v in value.items() if k != "seq"}


# ---------------------------------------------------------------------------
# Applying edits
# ---------------------------------------------------------------------------


def _append_at(body: str, at: int, text: str) -> str:
    """Insert ``text`` at ``at`` on a line of its own: a newline before it when the
    preceding text does not end one, and after it when more text follows."""
    head, tail = body[:at], body[at:]
    if head and not head.endswith("\n"):
        head += "\n"
    if tail and not text.endswith("\n"):
        text += "\n"
    return head + text + tail


def _section_end(body: str, start: int, end: int) -> int:
    """Where appended text goes in ``body[start:end]``: after its last non-blank
    line, so blank lines before the next heading stay where they are."""
    section = body[start:end]
    stripped = section.rstrip()
    if stripped == section:
        return end
    # Keep the newline that ends the last non-blank line inside the section.
    after = section[len(stripped) :]
    newline = after.find("\n")
    return start + len(stripped) + (newline + 1 if newline >= 0 else len(after))


def apply_edits(body: str, edits: list[dict[str, Any]]) -> str:
    """Apply validated ``edits`` to ``body`` in order.

    Args:
        body: The current markdown body.
        edits: Output of ``_edits_arg``.

    Returns:
        The edited body.

    Raises:
        ToolError: 400 naming the index of the first edit that cannot apply.
    """
    for index, edit in enumerate(edits):
        if "old" in edit:
            count = body.count(edit["old"])
            if count == 0:
                raise bad_request(
                    f"edits[{index}].old not found. Nothing was written; read the "
                    "article for its current text."
                )
            if count > 1 and not edit["replace_all"]:
                raise bad_request(
                    f"edits[{index}].old matches {count} times — add context or set "
                    "replace_all. Nothing was written."
                )
            body = body.replace(edit["old"], edit["new"])
        elif edit["section"] is None:
            body = _append_at(body, len(body), edit["append"])
        else:
            span = section_span(body, edit["section"])
            if span is None:
                raise bad_request(
                    f"edits[{index}].section: no heading matching '{edit['section']}'. "
                    "Nothing was written."
                )
            body = _append_at(body, _section_end(body, *span), edit["append"])
    return body


def _edits_apply(body: str, edits: list[dict[str, Any]]) -> bool:
    try:
        apply_edits(body, edits)
    except ToolError:
        return False
    return True


def _lean_stale(edits: list[dict[str, Any]]) -> Callable[[StoredObject], ToolError]:
    """The 409 for a stale ``if_version``: no body, only whether the edits still fit."""

    def build(current: StoredObject) -> ToolError:
        return conflict(
            STALE_MESSAGE,
            current_version=current.version,
            edits_apply=_edits_apply(parse(current.body).body, edits),
        )

    return build


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    if_version = version_arg(args)
    edits = _edits_arg(args)
    patch = _frontmatter_arg(args)
    if not edits and patch is None:
        raise bad_request("Give 'edits', 'frontmatter', or both.")

    require_grant(ctx, path, Permission.WRITE)

    s3 = ctx.minter.s3(ctx.subject, Shape.WRITE, path)
    st = store(ctx)
    current = live_for_update(st, s3, path)
    on_stale = _lean_stale(edits)
    if current.version != if_version:
        raise on_stale(current)

    stored = parse(current.body)
    content = apply_edits(stored.body, edits)
    # The stored block is re-checked against §10.2 before and after the merge.
    frontmatter = revalidate_stored(stored.frontmatter)
    if patch is not None:
        for key, value in patch.items():
            if value is None:
                frontmatter.pop(key, None)
            else:
                frontmatter[key] = value
        frontmatter = dict(validate_frontmatter(frontmatter, drop_seq=True))

    result = write_current(ctx, st, s3, path, current, frontmatter, content, on_stale)
    return {**result, "total_bytes": len(content.encode("utf-8"))}


TOOL = Tool(
    name="edit_article",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_WRITE,
    handler=handle,
)

__all__ = ["TOOL", "apply_edits", "handle"]
