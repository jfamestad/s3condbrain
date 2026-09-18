# mcp4me PRFAQ

2026-09-17 · Josh Famestad

## Press release

**mcp4me gives a team one permissioned knowledge tree its AI agents can read and write, with every change traced to a signed-in person.**

*A self-deployed remote MCP server. One organization per instance, on its own domain. Default deny; owners grant. First instance: wiki.famestad.com, September 2026.*

mcp4me is a tree of markdown articles with granular permissions, immutable version history, and a single write path: an agent acting for a person who has signed in. It is reachable from Claude (claude.ai and Claude Code) as a remote MCP server. Each organization runs its own instance on its own AWS account, holding one tree. An instance ships empty; what it is *for* is decided by what gets put in it.

**The problem.** Teams that share a vision ship faster, but the vision belongs to the team, not to one person, and not everyone should write every part of it. Today the choice is binary: give everyone the whole wiki, or share nothing. Agents make it worse. They can read a shared store, but nothing lets them write into one safely, with the writer's identity attached and the right to write scoped to what that person actually owns. Git is not for non-technical people; hosted wikis offer no per-user OAuth for a remote MCP client. Of 29 products evaluated, only one clears the remote-MCP-with-per-user-OAuth bar, and its licence forbids offering it as a service.

**The solution.** mcp4me makes the tree the permission model. A grant is *(person, node, permission)*; a folder grant cascades, an article grant applies to one article. Grants are positional: moving an article changes who can see it, and nothing else. Every version is immutable and attributed to the person whose session wrote it, in metadata an agent cannot rewrite. A person sees only what an owner granted; absence and denial are indistinguishable. Identity is delegated to WorkOS AuthKit, so PKCE, audience binding and revocation are correct by construction rather than by our code.

**What it is used for first.** A product decision register: Investor, Seller and Builder personas each own a namespace, each writes only its own records, everyone reads broadly, and an agent drafting a decision writes it as `proposed` for a human to accept. The second use is a household's records. Same software, different trees.

> "Broad read, governed write. That is the whole idea. Everybody can see the plan; only the person accountable for a part of it can change that part, and the agent helping them inherits exactly their rights and nothing more." — Josh Famestad, founder *(draft quote)*

> "I asked Claude why we chose the storage design and it read the decision record, the two it superseded, and the bet that paid for it. Nobody had to be in the room." — early user *(draft quote)*

**How to get started.** Add `https://wiki.famestad.com/mcp` as a connector in Claude, sign in through the organization's AuthKit login, and ask an owner for a grant. Deploying your own instance is `make deploy` against your AWS account plus a WorkOS environment; setup takes about a day.

## Customer FAQ

**How do I connect?** Add `https://<your-instance>/mcp` as a connector in claude.ai or Claude Code. Claude discovers the login server from the instance's metadata, sends you through a browser sign-in, and asks your consent once. No client secret, no API key.

**What can I see on day one?** Nothing. A new account sees no articles, no titles, not even that the tree exists. An owner grants you `read`, `write` or `own` on a folder or a single article, and that is what you see. Absence and denial look the same.

**Can I edit in a browser?** No. Reading in a browser comes later; writing is through the MCP tools only, by an agent acting for you. Every version records which signed-in person the agent was acting for.

**Can my agent grant access, invite someone, or delete things?** No. Creating people, changing grants, reading the audit log and hard-deleting are not tools. They live in the web application behind an interactive session, and the role the MCP server runs under cannot write to the grant table even if a tool tried. An agent that reads a malicious article cannot escalate its own access.

**What happens when I move an article?** Whoever can read the destination folder can now read it; whoever could read the old location cannot, but they find a pointer saying it moved and where. A move that gives anyone *new* access is refused on the tool surface and done in the web app, where you see who gains before confirming.

**If I delete a sentence, is it gone?** No. `read` on a path includes that path's whole history, so the old version still says what it said. To put text out of reach, move the article: the history stays at the old path under the old grants, and the new path starts fresh.

**Is there a permission model I have to learn?** Three verbs. `read` (the live article and its history), `write` (create, update, move, archive; implies read), `own` (everything, including granting, on that node and below). Folder grants cascade; grants add up; there are no deny rules. Restructure the tree instead.

**Who holds my data?** You do. The instance runs in your AWS account on your domain. Identity (the user directory, tokens, login) is hosted by WorkOS. Article bodies never leave your account.

**What does it cost to run?** For a household or small team, near zero: Lambda, S3, DynamoDB on demand, CloudFront, and WorkOS's free tier. No always-on compute. A custom login domain is the one paid option (US$99/month) and is not required.

**Does it work with tools other than Claude?** Any MCP client that supports OAuth with client ID metadata documents (CIMD) or dynamic registration. Claude is the only client tested for v1.

**Can I export everything?** Not in v1. An export is a complete copy carrying none of the permissions that protected it, so it needs scoping and disclosure logging first.

**Can an agent run unattended, overnight?** Not in v1. Every write is by a person in session. When unattended writers arrive, they come as an authenticated HTTP API beside the MCP endpoint, not as another connector.

## Internal FAQ

**Why build instead of buy?** Twenty-nine products were evaluated. Only Outline clears the remote-MCP-with-per-user-OAuth bar, and its licence forbids offering it as a service to third parties. Positional grants with owner delegation eliminate the rest. Build stands (HANDOFF §13.4).

**Why not use git instead?** Because ownership here is local and git's is not. The owner of a section delegates it: this folder to one agent with `write`, that folder to another, a worker who only needs to read gets `read` and nothing more, and the owner of `/racing/` can hand out those grants without asking the owner of `/`. Git's unit of access is the repository. A clone is the whole tree; the finest read grant any git host offers is the repo; write is all-or-nothing per repo; and there is no notion of a subtree owner who can grant within their subtree. Getting "read `/racing/` but not `/legal/`" out of git means splitting into repositories, at which point there is no one tree, no cross-links, and every delegation is a repo-admin task. Three more reasons, each smaller: git follows history across renames (`git log --follow`), which is exactly the disclosure the move rule prevents; git attribution is self-asserted unless every household member manages signing keys, where here the actor is server-set metadata the writer cannot forge; and history is rewritable by force-push, where here it is immutable under Object Lock. Markdown stays canonical, so the content can be exported to git any time. Git as the *permission model* is what is rejected.

**How does this improve on GitHub-style PRs for managing content?** A PR is a review gate on a change; the substrate's gate is a state on a record. An agent writes a decision as `status: proposed` and only a human sets `accepted` and stamps `verified`, so the human review PRs provide is kept, on the whole record rather than on a diff, and the reviewer reads it in the web view rather than in a diff tool. Where PRs are weaker for this job: `CODEOWNERS` says who must approve, not who may read, and GitHub cannot scope an agent's token to part of a repository; a positional `write` grant does both. Concurrency is optimistic per article, so there are no branches to keep in sync: a stale `if_version` returns `409` with the current body and the agent merges on the spot. Where PRs are stronger, honestly: a multi-file change is atomic and reviewed as one unit, and CI runs against it. v1 has no atomic multi-article change (a move is two writes; `batch` is ESC-0004) and no CI hook. If the content is code-shaped, needing many files to change together under tests, a PR is the right tool and this is not.

**Why is identity hosted at WorkOS when the register says Cognito?** The decision register (ADR-0009, ADR-0015, IDR-0005, all 4 Sep 2026) chose Cognito behind AgentCore Gateway. The 13 Sep handoff rejected both. Cognito supports neither CIMD nor DCR, omits `code_challenge_methods_supported` from discovery, exact-matches redirect URIs (breaking Claude Code's loopback port), and, decisively, AWS documents that revoked user-pool tokens still verify by signature. AgentCore Gateway invokes targets under its own service role with no subject or claims, so the substrate would be blind to who is calling. WorkOS AuthKit supports CIMD, resource indicators and revocation that revokes. The register has not been updated to say so; that is a gap to close, not a disagreement to relitigate.

**What does WorkOS cost us?** Identity is now a hosted dependency on the authorization path. Users sign in at a generated `*.authkit.app` host unless we pay US$99/month for a custom domain. And the product-phase story is unresolved: "each tenant deploys their own backend" conflicts with "our authorization server is a hosted SaaS". Deferred so v1 can ship (open item O-1).

**What is blocking right now?** Two things. CIMD is not yet enabled in the WorkOS staging environment, so the four gate checks in HANDOFF §2 have not run. And there is no AWS development account with credentials on the build machine. The code is built (skeleton plus increments A–G, 1,275 unit tests, synth-clean for dev and prod); it has not been deployed.

**What single check could reverse the auth decision?** Gate check 4: request a token for an unregistered resource URI. WorkOS documents that with no resource indicator configured the `resource` parameter is silently ignored and a default audience used. If check 4 issues a token instead of refusing, every instance on the account shares one audience and cross-organization isolation is gone. Stytch or Descope takes WorkOS's place; nothing downstream changes.

**Why S3 as the store and not DynamoDB?** S3 gives versioning, concurrency (conditional writes on ETag) and IAM enforcement natively. DynamoDB holds grants only. The accepted cost: a move is two writes and is not atomic; pointer-first ordering makes the failure mode "briefly unreadable at both paths" rather than "readable at both".

**How is agent compromise contained?** The storage credential is minted per *(subject, prefix, permission)* as an STS session policy and is incapable of reaching what the user cannot reach. No service role on the read path. The test: could a bug in application code leak anything? If yes, the credential is too strong. Grant-changing operations are not tools, and the MCP function's IAM role is read-only on the grant table.

**What is deliberately not in v1?** Unattended agents (Claude connectors have no machine-to-machine flow), users outside the organization, bundle export, event streams, full-text search, a human editing surface. Each is specified or reasoned about; none is built.

**Who is the first customer?** A household of four with no turnover, at wiki.famestad.com. The second is this project's own decision register, migrated from the hosted decision-wiki that currently holds it.

**What are the real limits on who can be given access?** The client, not the server. A Free Claude account holds one custom connector. Team and Enterprise members cannot self-serve a connector URL; an owner must add it organization-wide. Setup is web or desktop only. The instance must be reachable from Anthropic's egress range. And connector auth settings are fixed once added, so the authorization server is not quietly swappable after launch.

**What would kill it?** The gate failing with no substitute passing. Measured revocation latency that cannot be bounded to fifteen minutes end to end. The invitation email taking more than one screen for a person who has never heard of MCP. And the governance answer: IDR-0002's review date (11 Sep) has passed without a recorded review, and IDR-0005 (review 25 Sep) funds a design the code no longer implements. A bet whose review is skipped is not a bet.

**What does it cost to run?** Under US$5/month at household scale: Lambda inside the free tier, S3 and DynamoDB on demand, CloudFront, one KMS key. Nothing is always-on.
