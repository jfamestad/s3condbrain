"""Bootstrap the first owner: one row, ``own`` on ``/`` (HANDOFF §11.4, §15.1 step 3).

Runs with an operator's credentials, never the MCP role — that role cannot write
grants (§8.8), and this script failing under it is the expected outcome.

    uv run python scripts/grant_owner.py --subject user_01H... --table wiki-dev-grants
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.auth.grants import GrantStore
from app.auth.types import Grant, Permission


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--subject", required=True, help="WorkOS user id (token sub)")
    p.add_argument("--table", required=True)
    p.add_argument("--node", default="/")
    p.add_argument("--permission", default="own", choices=[x.value for x in Permission])
    a = p.parse_args()
    store = GrantStore(a.table)
    store.put_grant(
        Grant(
            subject=a.subject,
            node=a.node,
            permission=Permission(a.permission),
            granted_by="process:bootstrap",
            granted_at=datetime.now(UTC).isoformat(),
        )
    )
    print(f"granted {a.permission} on {a.node} to {a.subject}")


if __name__ == "__main__":
    main()
