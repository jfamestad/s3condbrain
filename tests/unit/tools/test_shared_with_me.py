"""``shared_with_me`` (s3condbrain S7)."""

from __future__ import annotations

from typing import Any

from app.auth.types import Permission
from app.config import SCOPE_READ
from app.mcp.protocol import ToolContext
from app.mcp.tools import registry
from app.mcp.tools.shared_with_me import TOOL
from tests.unit.tools.conftest import FakeMinter, call

FRIEND = "user_friend"


def test_descriptor() -> None:
    assert TOOL.name == "shared_with_me"
    assert TOOL.scope == SCOPE_READ
    assert "$ref" not in str(TOOL.descriptor())
    assert "shared_with_me" in registry()


def test_lists_only_the_callers_grants(
    make_ctx: Any, seed_grant: Any, ctx: ToolContext, minter: FakeMinter
) -> None:
    seed_grant(FRIEND, "/racing", Permission.READ)
    seed_grant(FRIEND, "/family/recipes/pie.md", Permission.WRITE)
    out = call(TOOL, make_ctx(FRIEND))
    assert out == {
        "grants": [
            {
                "path": "/family/recipes/pie.md",
                "permission": "write",
                "folder": False,
                "granted_by": "bootstrap",
                "granted_at": "2026-01-01T00:00:00Z",
            },
            {
                "path": "/racing",
                "permission": "read",
                "folder": True,
                "granted_by": "bootstrap",
                "granted_at": "2026-01-01T00:00:00Z",
            },
        ]
    }
    assert minter.mint_count == 0  # grant rows only; no S3


def test_no_grants_is_empty(nobody: ToolContext) -> None:
    assert call(TOOL, nobody) == {"grants": []}
