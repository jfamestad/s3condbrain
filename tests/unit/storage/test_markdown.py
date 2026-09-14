"""Frontmatter parse/serialize round-trips (HANDOFF §3.4)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.storage.markdown import (
    MAX_FRONTMATTER_BYTES,
    MAX_FRONTMATTER_DEPTH,
    parse,
    serialize,
)


def test_parse_splits_block_and_body() -> None:
    article = parse(b"---\ntype: doc\ntitle: Rear bar\nseq: 3\n---\n# Heading\n\nbody\n")
    assert article.frontmatter == {"type": "doc", "title": "Rear bar", "seq": 3}
    assert article.body == "# Heading\n\nbody\n"
    assert article.type == "doc"
    assert article.seq == 3


def test_parse_without_block_is_all_body() -> None:
    article = parse("just text\n---\nnot frontmatter\n")
    assert article.frontmatter == {}
    assert article.body == "just text\n---\nnot frontmatter\n"
    assert article.type == "doc"
    assert article.seq == 0


def test_parse_unterminated_block_is_all_body() -> None:
    raw = "---\ntype: doc\nno closing fence\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_parse_malformed_yaml_is_all_body() -> None:
    raw = "---\ntype: [unclosed\n---\nbody\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_parse_non_mapping_block_is_all_body() -> None:
    raw = "---\n- a\n- b\n---\nbody\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_parse_closing_fence_at_eof() -> None:
    article = parse("---\ntype: doc\n---")
    assert article.frontmatter == {"type": "doc"}
    assert article.body == ""


def test_parse_crlf_block() -> None:
    article = parse("---\r\ntype: doc\r\n---\r\nbody\r\n")
    assert article.frontmatter == {"type": "doc"}
    assert article.body == "body\r\n"


def test_parse_body_may_contain_horizontal_rules() -> None:
    article = parse("---\ntype: doc\n---\nabove\n\n---\n\nbelow\n")
    assert article.frontmatter == {"type": "doc"}
    assert article.body == "above\n\n---\n\nbelow\n"


def test_parse_coerces_yaml_timestamps_to_strings() -> None:
    article = parse(
        "---\ntype: doc\nverified:\n  - by: human:alice\n    at: 2026-01-02T03:04:05Z\n---\n"
    )
    entry = article.frontmatter["verified"][0]
    assert isinstance(entry["at"], str)
    assert entry["at"].startswith("2026-01-02T03:04:05")


def test_parse_replaces_undecodable_bytes() -> None:
    article = parse(b"---\ntype: doc\n---\nok \xff\n")
    assert article.frontmatter == {"type": "doc"}
    assert "ok" in article.body


def test_serialize_orders_type_first_seq_last() -> None:
    raw = serialize({"seq": 4, "zeta": 1, "type": "doc", "alpha": "x"}, "body\n")
    text = raw.decode()
    lines = text.split("\n")
    assert lines[0] == "---"
    assert lines[1] == "type: doc"
    assert lines[2] == "zeta: 1"
    assert lines[3] == "alpha: x"
    assert lines[4] == "seq: 4"
    assert lines[5] == "---"
    assert text.endswith("---\nbody\n")


def test_round_trip_preserves_unknown_keys_and_unicode() -> None:
    frontmatter = {
        "type": "doc",
        "title": "Réglage — barre arrière",
        "tags": ["racing", "setup"],
        "custom_key": {"nested": [1, 2, {"deep": "value"}]},
        "stale_after": "2026-12-01T00:00:00Z",
        "verified": [{"by": "human:alice", "at": "2026-01-01T00:00:00Z"}],
        "seq": 7,
    }
    body = "# Title\n\nSome *markdown* with unicode: café ☕\n\n---\n\nmore\n"
    article = parse(serialize(frontmatter, body))
    assert article.frontmatter == frontmatter
    assert article.body == body
    assert "Réglage" in serialize(frontmatter, body).decode()


def test_round_trip_empty_body() -> None:
    article = parse(serialize({"type": "doc", "seq": 1}, ""))
    assert article.frontmatter == {"type": "doc", "seq": 1}
    assert article.body == ""


def test_round_trip_body_with_leading_fence_like_lines() -> None:
    body = "---\nnot: frontmatter\n---\n"
    article = parse(serialize({"type": "doc"}, body))
    assert article.frontmatter == {"type": "doc"}
    assert article.body == body


# --- hostile frontmatter (review finding #6c) ----------------------------------------------
#
# Storage never refuses to read what it holds: every case below degrades to "empty
# frontmatter, whole text as body", exactly like a malformed block does today.


def _billion_laughs() -> str:
    lines = ['a: &a ["lol", "lol", "lol", "lol", "lol", "lol", "lol", "lol", "lol"]']
    prev = "a"
    for name in "bcdefghi":
        lines.append(f"{name}: &{name} [{', '.join(['*' + prev] * 9)}]")
        prev = name
    return "---\ntype: doc\n" + "\n".join(lines) + "\n---\nbody\n"


def test_billion_laughs_is_malformed_and_fast() -> None:
    raw = _billion_laughs()
    started = time.perf_counter()
    article = parse(raw)
    assert time.perf_counter() - started < 1.0
    assert article.frontmatter == {}
    assert article.body == raw


def test_any_alias_is_malformed() -> None:
    raw = "---\ntype: doc\nx: &x 1\ny: *x\n---\nbody\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_anchor_without_alias_is_harmless_but_still_refused_nowhere_else() -> None:
    # An anchor that is never referenced expands nothing; only aliases are refused.
    article = parse("---\ntype: doc\nx: &x 1\n---\nbody\n")
    assert article.frontmatter == {"type": "doc", "x": 1}


def _block_of_exactly(size: int) -> str:
    head = "type: doc\ntitle: "
    return head + "v" * (size - len(head) - 1) + "\n"


def test_block_at_the_byte_limit_parses() -> None:
    block = _block_of_exactly(MAX_FRONTMATTER_BYTES)
    assert len(block.encode()) == MAX_FRONTMATTER_BYTES
    article = parse(f"---\n{block}---\nbody\n")
    assert article.frontmatter["type"] == "doc"
    assert len(article.frontmatter["title"]) == MAX_FRONTMATTER_BYTES - len("type: doc\ntitle: \n")
    assert article.body == "body\n"


def test_block_one_byte_over_the_limit_is_malformed() -> None:
    block = _block_of_exactly(MAX_FRONTMATTER_BYTES + 1)
    raw = f"---\n{block}---\nbody\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_twenty_kib_block_is_malformed() -> None:
    padding = "".join(f"k{i}: {'v' * 90}\n" for i in range(220))
    assert len(padding.encode()) > 20 * 1024
    raw = f"---\ntype: doc\n{padding}---\nbody\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_byte_limit_counts_utf8_bytes_not_characters() -> None:
    # Three bytes per character: fewer characters than the limit, more bytes.
    block = "type: doc\ntitle: " + "☕" * (MAX_FRONTMATTER_BYTES // 2) + "\n"
    raw = f"---\n{block}---\nbody\n"
    assert len(block) < MAX_FRONTMATTER_BYTES < len(block.encode())
    assert parse(raw).frontmatter == {}


def test_nesting_at_the_depth_limit_parses() -> None:
    # The root mapping is one level; MAX_FRONTMATTER_DEPTH - 1 lists beneath it fit.
    inner = MAX_FRONTMATTER_DEPTH - 1
    article = parse(f"---\ntype: doc\nx: {'[' * inner}1{']' * inner}\n---\nbody\n")
    value: Any = article.frontmatter["x"]
    for _ in range(inner):
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
    assert value == 1


def test_nesting_one_past_the_depth_limit_is_malformed() -> None:
    inner = MAX_FRONTMATTER_DEPTH
    raw = f"---\ntype: doc\nx: {'[' * inner}1{']' * inner}\n---\nbody\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_pathological_nesting_never_raises() -> None:
    # Deep enough to exhaust Python's stack in the composer without the guard.
    raw = f"---\ntype: doc\nx: {'[' * 5000}1{']' * 5000}\n---\nbody\n"
    article = parse(raw)
    assert article.frontmatter == {}
    assert article.body == raw


def test_nested_mappings_count_toward_depth() -> None:
    inner = MAX_FRONTMATTER_DEPTH
    raw = "---\ntype: doc\nx: " + "{a: " * inner + "1" + "}" * inner + "\n---\nbody\n"
    assert parse(raw).frontmatter == {}


def test_ordinary_documents_are_unaffected() -> None:
    frontmatter = {
        "type": "doc",
        "title": "Rear bar",
        "tags": ["racing", "setup"],
        "sources": [{"resource": "https://example.com", "id": "x"}],
        "verified": [{"by": "human:alice", "at": "2026-01-01T00:00:00Z"}],
        "custom": {"nested": {"deeper": [1, 2, {"deepest": True}]}},
        "seq": 12,
    }
    body = "# Title\n\ntext\n"
    article = parse(serialize(frontmatter, body))
    assert article.frontmatter == frontmatter
    assert article.body == body
    assert article.seq == 12


# --- Article.seq is tolerant (review finding #6d) ------------------------------------------


@pytest.mark.parametrize(
    "value",
    ['"abc"', "true", "false", "[1, 2]", "{a: 1}", "null", "''", ".nan", "1.0e+400", "1e400"],
)
def test_non_integer_seq_reads_as_zero(value: str) -> None:
    """A non-numeric ``seq`` in any stored version must not 500 every history page."""
    article = parse(f"---\ntype: doc\nseq: {value}\n---\nbody\n")
    assert article.frontmatter["type"] == "doc"
    assert article.seq == 0


@pytest.mark.parametrize(("value", "expected"), [("7", 7), ('"7"', 7), ("0", 0)])
def test_integer_like_seq_still_reads(value: str, expected: int) -> None:
    assert parse(f"---\ntype: doc\nseq: {value}\n---\n").seq == expected
