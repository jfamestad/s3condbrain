"""Grant a permission from the command line (HANDOFF §11.4, §15.1 step 3).

Runs with an operator's credentials, never the MCP role — that role cannot write
grants (§8.8), and this script failing under it is the expected outcome.

Every grant goes through ``GrantAdmin`` and its owner guard (§4.10): ``--granter``
must hold ``own`` on the node or an ancestor. The one exception is the very first
row — ``own`` on ``/`` in an empty table — which nothing can vouch for because
there is no owner yet. ``--bootstrap`` bypasses the guard for exactly that case
and logs loudly. Use it once per instance.

    # first owner, empty table
    uv run python scripts/grant_owner.py --bootstrap --subject user_01H... --table wiki-dev-grants

    # thereafter, as an owner
    uv run python scripts/grant_owner.py --granter user_01H... --subject user_02J... \\
        --node /racing --permission read --table wiki-dev-grants
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.auth.admin import GrantAdmin, NotAnOwner, logger
from app.auth.types import Grant, Permission

BOOTSTRAP_GRANTER = "process:bootstrap"


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--subject", required=True, help="WorkOS user id (token sub) receiving the grant"
    )
    p.add_argument("--table", required=True)
    p.add_argument("--node", default="/")
    p.add_argument("--permission", default="own", choices=[x.value for x in Permission])
    p.add_argument("--granter", help="subject of the owner making the grant (token sub)")
    p.add_argument(
        "--email", help="with --bootstrap: the first owner's email, for their PROFILE row"
    )
    p.add_argument("--name", default="", help="with --bootstrap: display name for the PROFILE row")
    p.add_argument(
        "--bootstrap",
        action="store_true",
        help="bypass the owner guard for the first `own /` row in an empty table",
    )
    a = p.parse_args(argv)
    if a.bootstrap and a.granter:
        p.error("--bootstrap and --granter are mutually exclusive")
    if not a.bootstrap and not a.granter:
        p.error("--granter is required unless --bootstrap")
    if a.bootstrap and (a.node != "/" or a.permission != Permission.OWN.value):
        p.error("--bootstrap only writes `own` on `/`")
    if a.bootstrap and not a.email:
        p.error("--bootstrap needs --email so the first owner can sign in to the web app")
    return a


def _bootstrap(admin: GrantAdmin, subject: str, email: str = "", name: str = "") -> Grant:
    """Write ``own /`` without an owner to vouch for it, plus the PROFILE row the
    web application requires at login (default deny, §3.3).

    Raises:
        NotAnOwner: ``/`` already has an owner — once there is one, they are the
            granter, and the guard has something to check against.
    """
    existing = [g.subject for g in admin.grants_on("/") if g.permission is Permission.OWN]
    if existing:
        raise NotAnOwner(f"/ already has owner(s) {existing}; use --granter instead")
    grant = Grant(
        subject=subject,
        node="/",
        permission=Permission.OWN,
        granted_by=BOOTSTRAP_GRANTER,
        granted_at=datetime.now(UTC).isoformat(),
    )
    admin.store.put_grant(grant)
    if email and admin.get_profile(subject) is None:
        admin.create_profile(subject, email, name or email, "active")
    logger.warning(
        "admin_write",
        action="bootstrap",
        granter=BOOTSTRAP_GRANTER,
        subject=subject,
        node="/",
        permission=Permission.OWN.value,
        note="OWNER GUARD BYPASSED — first owner of an empty table",
    )
    return grant


def main(argv: Sequence[str] | None = None, dynamodb_resource: Any | None = None) -> int:
    a = _parse(argv)
    admin = GrantAdmin(a.table, dynamodb_resource=dynamodb_resource)
    try:
        if a.bootstrap:
            print("BOOTSTRAP: bypassing the owner guard for the first `own /` row", file=sys.stderr)
            grant = _bootstrap(admin, a.subject, a.email or "", a.name or "")
        else:
            grant = admin.grant(a.granter, a.subject, a.node, Permission(a.permission))
    except NotAnOwner as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"granted {grant.permission.value} on {grant.node} to {grant.subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
