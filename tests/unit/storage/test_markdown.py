"""Frontmatter parse/serialize round-trips (HANDOFF §3.4)."""

from __future__ import annotations

from app.storage.markdown import parse, serialize


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
