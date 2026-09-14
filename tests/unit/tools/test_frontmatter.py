"""``validate_frontmatter`` enforces the advertised §10.2 limits server-side (review
finding #6a). Every violation is a 400 naming the field, and anything it accepts
round-trips through storage — the write-side limits sit inside the read-side ones."""

from __future__ import annotations

from typing import Any

import pytest

from app.errors import ToolError
from app.mcp.tools._common import (
    MAX_FRONTMATTER_BYTES,
    MAX_FRONTMATTER_DEPTH,
    MAX_FRONTMATTER_KEYS,
    validate_frontmatter,
)
from app.storage.markdown import parse, serialize

BASE: dict[str, Any] = {"type": "doc"}


def _rejects(value: Any, field: str, **kwargs: Any) -> ToolError:
    with pytest.raises(ToolError) as info:
        validate_frontmatter(value, **kwargs)
    assert (info.value.status, info.value.code) == (400, "bad_request"), info.value.structured()
    assert field in info.value.message, info.value.message
    return info.value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("title", "t" * 200),
        ("title", ""),
        ("description", "d" * 500),
        ("tags", ["x" * 60] * 20),
        ("tags", []),
        ("status", "draft"),
        ("status", "stable"),
        ("status", "deprecated"),
        ("stale_after", "2027-01-01T00:00:00Z"),
        (
            "sources",
            [{"resource": "r" * 2048, "id": "i" * 64, "title": "t" * 200, "author": "a" * 200}],
        ),
        ("sources", [{"resource": "https://example.com/x"}] * 50),
        ("sources", [{"resource": "r", "extra": "unknown source keys pass"}]),
        ("generated", {"by": "process:indexer", "at": "2026-01-01T00:00:00Z"}),
        ("generated", {"by": "claude/4.1"}),
        ("verified", [{"by": "human:alice@example.com"}] * 50),
        ("verified", [{"by": "human:alice", "at": "2026-01-01T00:00:00Z"}]),
        ("verified", []),
        ("custom", {"anything": [1, 2.5, {"deep": None, "flag": True}]}),
    ],
)
def test_accepts_fields_at_their_limits(key: str, value: Any) -> None:
    fm = {**BASE, key: value}
    assert validate_frontmatter(fm) == fm


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("title", "t" * 201),
        ("title", 7),
        ("description", "d" * 501),
        ("description", ["x"]),
        ("tags", ["x" * 61]),
        ("tags", ["x"] * 21),
        ("tags", "not-a-list"),
        ("tags", [1]),
        ("tags", [None]),
        ("status", "published"),
        ("status", 1),
        ("stale_after", 20270101),
        ("stale_after", ["2027"]),
        ("sources", "x"),
        ("sources", ["r"]),
        ("sources", [{"id": "no resource"}]),
        ("sources", [{"resource": 5}]),
        ("sources", [{"resource": "r" * 2049}]),
        ("sources", [{"resource": "r", "id": "i" * 65}]),
        ("sources", [{"resource": "r", "id": 1}]),
        ("sources", [{"resource": "r", "title": "t" * 201}]),
        ("sources", [{"resource": "r", "author": "a" * 201}]),
        ("sources", [{"resource": "r"}] * 51),
        ("generated", "process:x"),
        ("generated", {}),
        ("generated", {"by": "not an actor"}),
        ("generated", {"by": "human:" + "a" * 260}),
        ("generated", {"by": "human:a", "at": 5}),
        ("verified", {"by": "human:a"}),
        ("verified", ["human:a"]),
        ("verified", [{"by": "nope"}]),
        ("verified", [{"at": "2026-01-01T00:00:00Z"}]),
        ("verified", [{"by": "human:a"}] * 51),
    ],
)
def test_rejects_violations_naming_the_field(key: str, value: Any) -> None:
    _rejects({**BASE, key: value}, f"frontmatter.{key}")


def test_key_count_is_capped() -> None:
    fm = {"type": "doc", **{f"k{i}": i for i in range(MAX_FRONTMATTER_KEYS - 1)}}
    assert len(fm) == MAX_FRONTMATTER_KEYS
    assert validate_frontmatter(fm) == fm
    _rejects({**fm, "one_more": 1}, "frontmatter")


def test_serialized_block_is_capped() -> None:
    _rejects({"type": "doc", "notes": "n" * MAX_FRONTMATTER_BYTES}, "frontmatter")
    ok = {"type": "doc", "notes": "n" * (MAX_FRONTMATTER_BYTES - 200)}
    assert validate_frontmatter(ok) == ok


def test_block_limit_counts_utf8_bytes() -> None:
    _rejects({"type": "doc", "notes": "☕" * (MAX_FRONTMATTER_BYTES // 2)}, "frontmatter")


def test_largest_accepted_block_still_parses_after_the_server_adds_seq() -> None:
    """The write-side cap must leave room for the server's ``seq`` line, or a
    boundary-sized article would be stored and then read back as malformed."""
    low, high = 0, MAX_FRONTMATTER_BYTES
    while low < high:  # largest n such that validation accepts "n" * n
        mid = (low + high + 1) // 2
        try:
            validate_frontmatter({"type": "doc", "notes": "n" * mid})
        except ToolError:
            high = mid - 1
        else:
            low = mid
    assert low > MAX_FRONTMATTER_BYTES - 100
    stored = serialize({"type": "doc", "notes": "n" * low, "seq": 10**15}, "body\n")
    article = parse(stored)
    assert article.frontmatter == {"type": "doc", "notes": "n" * low, "seq": 10**15}
    assert article.body == "body\n"


def test_nesting_is_capped_at_the_parser_depth() -> None:
    nested: Any = "leaf"
    for _ in range(MAX_FRONTMATTER_DEPTH - 1):  # the root mapping is one level
        nested = [nested]
    ok = {"type": "doc", "x": nested}
    assert validate_frontmatter(ok) == ok
    assert parse(serialize({**ok, "seq": 1}, "")).frontmatter == {**ok, "seq": 1}
    _rejects({"type": "doc", "x": [nested]}, "frontmatter")


def test_nested_mappings_count_toward_depth() -> None:
    nested: Any = "leaf"
    for _ in range(MAX_FRONTMATTER_DEPTH):
        nested = {"a": nested}
    _rejects({"type": "doc", "x": nested}, "frontmatter")


def test_unserialisable_value_is_400_not_500() -> None:
    _rejects({"type": "doc", "x": object()}, "frontmatter")


def test_seq_rejected_on_create_dropped_on_update() -> None:
    _rejects({"type": "doc", "seq": 3}, "frontmatter.seq")
    _rejects({"type": "doc", "seq": "x"}, "frontmatter.seq")
    assert validate_frontmatter({"type": "doc", "seq": "x"}, drop_seq=True) == {"type": "doc"}
    assert validate_frontmatter({"type": "doc", "seq": 4}, drop_seq=True) == {"type": "doc"}


def test_does_not_mutate_the_caller_mapping() -> None:
    fm = {"type": "doc", "seq": 4, "title": "x"}
    validate_frontmatter(fm, drop_seq=True)
    assert fm == {"type": "doc", "seq": 4, "title": "x"}
