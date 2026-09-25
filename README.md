# s3condbrain

**A shared knowledge base your AI agents can read and write, where every person sees
and changes only what they've been given.**

s3condbrain is a tree of markdown articles that people reach through their AI agent,
connected to Claude as a remote MCP server. You ask Claude a question and it searches
and reads the tree for you. You ask it to record something and it writes an article,
attributed to you. Access is set per folder or per article: an owner gives someone
`read`, `write` or `own` on part of the tree, and that part is all they, and their
agent, can see or change.

It runs on your own AWS account (S3, DynamoDB, Lambda), with WorkOS handling sign-in.
There are no servers to run, every change is kept, and nothing is ever deleted by
accident.

*Formerly **mcp4me**; older design documents use that name and "wiki-substrate".*

## Why

Teams and families want one shared memory their agents can use, but not everyone
should see or edit all of it. Ordinary wikis make that a choice between sharing the
whole wiki and sharing nothing, and they give an agent no safe way to write with the
writer's identity attached. s3condbrain makes **the tree itself the permission
model**:

- **Broad read, governed write.** Everyone can read the plan; only the person
  accountable for a part can change that part. The agent helping them has exactly
  their rights and nothing more.
- **Every version is kept and attributed** to the signed-in person the agent was
  acting for, in metadata the agent can't rewrite.
- **Default deny.** A new person sees nothing, not even that the tree exists, until
  an owner grants them something. Content they can't see and content that doesn't
  exist look the same to them.

Typical uses: a product team's decision records (each role owns its own section and
everyone reads across), a household's records, a small business's shared knowledge.
An instance starts empty; what it's for is whatever you put in it.

## Using an instance

You need a Claude account (web, desktop or Claude Code), and someone who owns the
instance needs to grant you access.

1. **Connect.** In Claude, go to Settings → Connectors → Add custom connector and
   enter `https://<instance>/mcp`. Sign in with the emailed link. Do this on web or
   desktop; once connected it also works on your phone. In Claude Code, add the
   same URL as a remote MCP server.
2. **Ask an owner for access.** They add you in the admin console and grant you a
   folder or an article.
3. **Talk to Claude.** It picks the right tools itself. For example:

| You say | What happens |
|---|---|
| "What did we decide about the storage design, and why?" | `search_and_read` finds the decision record and returns it in one call |
| "Add a row to the roadmap's open questions for the pricing question." | `edit_article` appends the row; it doesn't resend the whole page |
| "Draft an ADR for the new search design as proposed." | `create_article` writes it under your name, as a draft |
| "Move the old notes into /archive/2025." | `move_article` moves it and leaves a pointer at the old path |
| "What's been shared with me? Put Scott's racing folder in my tree." | `shared_with_me` lists your grants; a `link` article places the folder in your own tree |
| "What did this page say last month?" | `list_versions` and `read_version` show the history |

### Concepts in two minutes

- **Tree.** Folders and markdown articles with YAML frontmatter (title, description,
  tags, status), in the Open Knowledge Format (OKF) shape.
- **Grants.** `read`, `write` or `own` on a folder (which covers everything beneath
  it) or on a single article. Grants add up; there are no deny rules. Only people
  holding `own` grant access, and only in the web console. No agent can grant.
- **Versions.** Every write is a new, immutable version. **Archive** hides an article
  without destroying anything. **Move** leaves a forwarding pointer, and since access
  follows position, moving something changes who can see it (a move that would widen
  access has to be confirmed by a person in the console).
- **Links.** A `link` article points at a folder or article shared with you, so your
  tree becomes your own content plus the things others have shared. A link grants
  nothing: the target still checks your access every time.
- **The web app** at `https://<instance>/app`: browse and search what you can read,
  view history, and, for owners, the admin console (add people, grant and revoke,
  the audit log, confirming moves that widen access).

### What agents can do

Fourteen MCP tools. Every read is a search or a targeted fetch; results are bounded
so they fit in an agent's context.

| Find | Read | Write | History | Sharing |
|---|---|---|---|---|
| `search` | `read_article` (whole, one section, or a byte range) | `create_article` | `list_versions` | `shared_with_me` |
| `search_and_read` | `list_folder` | `edit_article` (patch edits) | `read_version` | `resolve_reference` |
| | | `update_article` (full replace) · `move_article` · `archive_article` · `unarchive_article` | | |

Agents can't grant access, add people, read the audit log or hard-delete. Those
exist only in the web console, and the role the MCP server runs under can't write
grants even if a tool tried.

## Running your own instance

**Start with [docs/SETUP.md](docs/SETUP.md).** It walks through setting up WorkOS,
deploying the four AWS stacks with CDK, and connecting the two, ending with Claude
reading your tree. [docs/DEPLOY.md](docs/DEPLOY.md) is the full reference, including
production differences; [docs/RUNBOOK.md](docs/RUNBOOK.md) covers restore,
break-glass, revoking access and alarms.

You need an AWS account, a WorkOS account, a domain you control, and about a day for
the first setup.

**Security, briefly:**
- Each operation gets short-lived AWS credentials scoped to the one path it touches,
  so a bug can't read past that path.
- Tokens are bound to your instance's URL.
- The bucket is encrypted with your own KMS key, with optional Object Lock in
  production.
- Every read, write, grant and denial is audited.
- The design and the threat model are in [HANDOFF.md](HANDOFF.md).

## Status

- **v1 is built and running:** one organization per instance, people added by an
  owner, every write made by an agent acting for a signed-in person.
- **In development:** semantic search over article bodies (S3 Vectors),
  subscriptions (your agent tells you when something you watch changes), and a
  hosted multi-tenant version.
- **The design documents** are [docs/PRFAQ.md](docs/PRFAQ.md) (the product),
  [docs/PRD.md](docs/PRD.md) (requirements), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
  (the system) and [HANDOFF.md](HANDOFF.md) (the authoritative spec). Where code and
  HANDOFF disagree, fix the code.

## Developing

| Path | What |
| --- | --- |
| `app/authorizer/` | API Gateway REQUEST authorizer: JWT validation, nothing else |
| `app/mcp/` | MCP transport (`server.py`) and the fourteen tools (`tools/`) |
| `app/auth/` | Grant resolution, per-operation credential minting, grant administration, rate limits |
| `app/storage/` | S3 reads and conditional writes, frontmatter, the listing projection |
| `app/web/` | The web application: login, read view, admin console |
| `infra/` | CDK: storage, compute, api, ops stacks |
| `scripts/` | `oauth_gate.py` (the WorkOS gate), `grant_owner.py` (bootstrap the first owner) |
| `tests/unit/` | Unit tests, no AWS (moto) |
| `tests/security/` | The security checklist as integration tests against a deployed instance |
| `docs/` | `SETUP.md` (start here), `DEPLOY.md` (reference), `RUNBOOK.md` (operations), design docs, `plans/` |

```
make sync        # dependencies
make test        # unit tests
make lint        # ruff
make typecheck   # mypy
make build       # package the Lambda functions (arm64, py3.13)
make synth ENV=dev
make deploy ENV=dev
make security    # after deploy; env contract in tests/security/README.md
make aws-tests   # the storage-isolation negative tests, with real credentials
```

Changes go through pull requests. Read HANDOFF §1, §2, §4 and §11 before touching
authorization or storage.

## License

Apache License 2.0; see [LICENSE](LICENSE).
