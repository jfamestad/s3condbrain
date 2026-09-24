# wiki-substrate

A permissioned, versioned tree of markdown articles, managed by agents through a
remote MCP server, with a small web application for the humans who own it.

The design is `HANDOFF.md`. Read §1, §2, §4, §11 first; everything else when you
touch it. Where code and document disagree, fix the code.

## Layout

| Path | What |
| --- | --- |
| `app/authorizer/` | API Gateway REQUEST authorizer — JWT validation, nothing else |
| `app/mcp/` | MCP transport (`server.py`) and the eleven tools (`tools/`) |
| `app/auth/` | Grant resolution, per-operation credential minting, grant administration, rate limits |
| `app/storage/` | S3 reads and conditional writes, frontmatter, the listing projection |
| `app/web/` | The web application: login, read view, admin console |
| `infra/` | CDK: storage, compute, api, ops stacks |
| `scripts/` | `oauth_gate.py` (the §2 gate), `grant_owner.py` (bootstrap) |
| `tests/unit/` | Unit tests, no AWS (moto) |
| `tests/security/` | §12.8 as integration tests against a deployed instance |
| `docs/` | `DEPLOY.md` (zero to production), `RUNBOOK.md` (restore, break-glass, revoke, alarms) |

## Commands

    make sync        # dependencies
    make test        # unit tests
    make lint        # ruff
    make typecheck   # mypy
    make build       # package the Lambda functions (arm64, py3.13)
    make synth ENV=dev CERT_ARN=arn:aws:acm:...
    make deploy ENV=dev CERT_ARN=arn:aws:acm:...
    make security    # after deploy; env contract in tests/security/README.md
    make aws-tests   # the §4.8 negative tests, with real credentials

## Before the first deploy

1. WorkOS: enable CIMD, register the resource, define the scopes, create the
   web app's own client — then `uv run python scripts/oauth_gate.py ...` must
   print `GATE PASSED`. Details in `docs/DEPLOY.md`.
2. AWS: a development account with credentials configured, and either a Route 53
   hosted zone in `infra/config.py` or an ACM certificate for the domain.
3. `infra/config.py`: `authkit_domain`, `domain`, `workos_web_client_id`.

Then `make deploy`, point DNS at the `DomainTarget` output, fill the web secret
(`WebSecretArn` output) with the AuthKit client's `client_secret` — the only
WorkOS credential any function holds; there is no management API key to fill —
`make grant-owner SUBJECT=user_... TABLE=...`, and add the connector in Claude.
