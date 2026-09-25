"""Links (s3condbrain S7): validation and the one-hop reference."""

from __future__ import annotations

import pytest

from app.errors import ToolError
from app.mcp.tools._links import (
    LINK_TO,
    TYPE_LINK,
    check_link,
    is_local,
    link_reference,
    link_target,
)


@pytest.mark.parametrize(
    "target",
    [
        "/racing",
        "/",
        "/racing/setup/rear-bar.md",
        "https://wiki.acme.com/a/standards/torque-spec.md",
    ],
)
def test_valid_targets(target: str) -> None:
    assert link_target(target) == target


@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        "racing",
        "/Racing",
        "/racing/",
        "/_sys/x.md",
        "/x.md/y.md",
        "http://wiki.acme.com/a/x.md",
        "https://wiki.acme.com/racing",  # folder URLs are not yet in the §7 grammar
        "https://user@wiki.acme.com/a/x.md",
    ],
)
def test_invalid_targets_are_400(target: object) -> None:
    with pytest.raises(ToolError) as info:
        link_target(target)
    assert info.value.status == 400


def test_check_link_requires_target_on_links() -> None:
    with pytest.raises(ToolError):
        check_link({"type": TYPE_LINK})
    check_link({"type": TYPE_LINK, LINK_TO: "/racing"})


def test_check_link_rejects_target_on_non_links() -> None:
    with pytest.raises(ToolError):
        check_link({"type": "doc", LINK_TO: "/racing"})
    check_link({"type": "doc"})


def test_is_local() -> None:
    assert is_local("/racing")
    assert not is_local("https://wiki.acme.com/a/x.md")


def test_link_reference_shape() -> None:
    ref = link_reference("/me/racing.md", {"type": TYPE_LINK, LINK_TO: "/racing"})
    assert ref["kind"] == "link"
    assert ref["path"] == "/me/racing.md"
    assert ref["link_to"] == "/racing"
    assert "list_folder" in ref["note"]
