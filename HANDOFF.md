# Wiki Substrate — Engineering Handoff

**Instance:** `wiki.famestad.com` · **Date:** 13 September 2026 · **Status:** v1 built (skeleton + increments A–G, 1,002 unit tests, synth-clean for dev and prod); **deployment blocked on the §2 gate** (CIMD not yet enabled in WorkOS) and an AWS development account

A deployable knowledge server: one permissioned, versioned tree of markdown articles per organization, managed by agents, reachable as a remote MCP server. What an instance is *for* is decided by what gets put in it.

---

## 0. How to use this document

This is the single authoritative reference for building v1. It consolidates and supersedes five design documents:

| Source | Version | What it contributed |
| --- | --- | --- |
| Wiki Substrate Spec | v0.12 | Behaviour, permissions, protocol conformance, security |
| Wiki Substrate Storage | v0.3 | S3 + DynamoDB mechanism |
| Authorization Server ADR | Builder, 13 Sep 2026 | WorkOS decision and the rejected field |
| Wiki Substrate Tools | v0.1 | Wire contract and data model |
| Wiki Substrate Build | v1 | Hosting, build order, decision register |

Where this document and any source disagree, **this document wins**. Do not re-litigate anything in §13 without reading why it was rejected.

**Conventions.** MUST / MUST NOT / SHALL mark requirements that carry a security property; breaking one is a defect, not a preference. SHOULD marks a strong default. Everything else is description.

**Reading order for a new contributor:** §1, §2, §4, §11. Then the section covering whatever you are about to write.

**Sections that encode security properties:** §4.5, §4.6, §4.7, §4.8, §4.9, §5.3, §6.2, §6.4, §8.1, §8.5, §8.8, §12. A change to any of these warrants a decision record, not an edit.

**Revision note (13 Sep 2026, post-review).** The storage mechanics in §5, §8 and §10 were brought into agreement: archive is a written tombstone rather than a delete marker, moves are pointer-first, pointer chains are followed by the caller one hop at a time, and actor attribution lives in server-set object metadata. The compute layout is two functions. Scope errors are HTTP-level; grant errors are tool errors. Decisions are recorded inline where they apply and summarised in §10.15 and §13.

---

## 1. What you are building

### 1.1 Shape

A tree of markdown articles with granular permissions, immutable version history, and a single write path: an agent acting for a signed-in person. Each organization runs its own instance, on its own infrastructure and its own domain, holding one tree.

**The deliverable is the server, not the content.** A family's records and a product's decision handbook are the same software with different trees — the content defines the purpose. There is deliberately no seeding, templating, or starter-content story: an instance ships empty.

### 1.2 Non-goals

- **Not a human editor.** No web page where a person types article content. Writes arrive through the MCP tool surface only.
- **Not git.** Markdown is canonical; git is explicitly not the store, so version history is built here rather than inherited.
- **Not multi-tenant SaaS.** One deployment serves one organization.
- **Not federated identity.** No cross-instance token exchange, no trust between authorization servers (§7).
- **No unattended agents in v1.** Every write traces to a person in session. Claude connectors do not support machine-to-machine flows at all, so when unattended writers return they arrive as an authenticated HTTP API beside the MCP endpoint, not as another connector.
- **No bundle export in v1.** An export is a complete copy of the tree carrying none of the permissions that protected it — the §12.5 problem as a one-click feature. Full OKF conformance is therefore not a v1 requirement.
- **No users outside the organization in v1.** Every principal is a member of the operating family or team. Invitations to outsiders, and the cross-instance references of §7 that depend on them, are deferred to v2. The design is retained and specified; only the build scope excludes it. This removes invitation onboarding, cross-instance resolution, the managed-org access gap, and the entire class of untrusted content arriving from another organization.

### 1.3 Scale

Hundreds of articles today; nothing in the design forecloses ten thousand. First tenant is a household of four with no turnover. Operational burden must stay near zero.

---

## 2. Start here — the blocking gate

**Nothing else in this document should begin until these checks pass.** They take about half an hour against a free WorkOS staging environment, and `scripts/oauth_gate.py` runs checks 1, 3, 4 and 5 as one command (two browser rounds) and prints reminders for the rest. Checks 1–4 are the gate proper and the fourth one can reverse the entire authorization decision; checks 5–7 confirm assumptions the rest of the design leans on. Run them against the **development** instance URL (§9.6) — production is registered separately at increment G.

1. **Metadata.** Fetch the AuthKit authorization-server metadata. Confirm it advertises **both** `client_id_metadata_document_supported: true` **and** `none` in `token_endpoint_auth_methods_supported`. Claude requires both to select CIMD; with either missing it silently falls back to hunting for a registration endpoint.
2. **Registration.** Register `https://wiki-dev.famestad.com/mcp` as a resource indicator.
3. **Round trip.** Complete an authorization-code + PKCE flow with `resource=https://wiki-dev.famestad.com/mcp`. Decode the access token. `aud` MUST be exactly that string.
4. **Negative case.** Request a token for a resource URI that was **not** registered. It MUST be **refused**.
5. **Scopes.** The token from check 3 carries `wiki.read` and `wiki.write` in its `scope` claim. §6.5 depends on custom scopes being issued, not merely configured.
6. **CIMD allowlist.** The dashboard can restrict which client-metadata origins are accepted. If it cannot, §4.9's last row is a wish rather than a control — note it and continue.
7. **Self-signup.** AuthKit self-registration can be disabled for the environment. Default deny (§3.3) covers the substrate either way, but a stranger should not be able to obtain a valid token and a working, empty connector.

### Why check 4 decides it

WorkOS documents that when no resource indicator is configured, the `resource` parameter is *ignored* and an environment-default audience is used instead. That failure is silent: a valid token, a wrong audience, no error anywhere, and nothing downstream that can detect it. Every instance on the account would share one audience and cross-organization isolation would disappear.

If check 4 issues a token rather than refusing, the WorkOS decision is rejected and **Stytch or Descope takes its place** — Descope requires `resource` at both endpoints, so the same mistake fails loudly there. Nothing downstream of the authorization server is affected by the substitution (§4.1).

---

## 3. Model and vocabulary

| Term | Definition |
| --- | --- |
| **Instance** | One deployment, one organization, one root URL. Owns exactly one tree and one authorization server. |
| **Resource** | A node in the tree: either a *folder* or an *article*. Addressed by an absolute path, e.g. `/racing/setup/rear-bar.md`. |
| **Version** | An immutable snapshot of one article's body and frontmatter, plus its author and timestamp. Versions are never modified or deleted. |
| **Principal** | A user. There is only one kind (§3.3). |
| **Grant** | A tuple of *(principal, node, permission)*. The node may be a folder or a single article. |
| **Pointer** | A tombstone version at a vacated path recording where the content went (§5.3). |

### 3.1 Path grammar

Paths are slash-separated and **lowercase**. The tool schemas (§10.2) reject anything else; nothing normalizes — a rejected call is visible to the agent, a silently rewritten one is not. Paths MUST NOT contain `.` or `..` segments. `index.md` and `log.md` are reserved names and MUST be rejected on create. Segments MUST NOT begin with `_` (reserved for system objects).

```
/                         tree root
/racing/                  folder
/racing/setup/            folder
/racing/setup/rear-bar.md article
```

Any node may be the target of a grant: a folder grant cascades to everything beneath it, an article grant applies to that article alone.

### 3.2 The tree is the manual

There is no ordering field, no table-of-contents object, no sequence metadata. The tree carries the structure and the prose carries the argument: an article's position says where it belongs, and its links say what to read next.

This makes the hierarchy serve two masters at once. The same tree that expresses logical structure also carries ownership and permission — a section split for narrative reasons is also a permission boundary, and a subtree drawn around an audience is also a chapter. **That is the intended constraint, not an accident:** one structure to reason about instead of two that drift apart.

*Consequence for the authoring surface:* because structure and permission are the same decision, the UI must show the permission consequence at the moment structure is chosen. By the time a tree is shaped wrongly, both jobs are wrong at once.

### 3.3 Principals

There is one kind of principal: a **user**. Someone inside the organization and someone invited from outside are the same object holding different grants. There is no guest class and no privilege that attaches to being internal — position in the permission graph is the only thing that differs.

Users authenticate interactively: authorization code flow with PKCE, through a browser. Every write records the user who made it.

**Default deny, without exception.** A newly created user sees nothing — not article bodies, not titles, not the shape of the tree, not the fact that anything exists. Everything reachable is reachable because an owner granted it. Nothing is public and there is no anonymous read path.

The single exception is structural: the two discovery documents in §4.2 MUST be publicly fetchable, because that is how a client learns where to authenticate. They disclose the instance's resource URI and issuer, and nothing else.

### 3.4 Open Knowledge Format alignment

Articles are OKF concepts (OKF v0.2). The format was arrived at independently on the two points that matter most — **a concept's identity *is* its path minus the `.md` suffix**, and relationships are ordinary markdown links whose meaning comes from surrounding prose — so adoption costs nothing structural.

**Type vocabulary.** Starting set is deliberately one value: `doc`. A vocabulary that starts small can grow; one that starts speculative leaves dead values in files forever. Two values are reserved and written only by the server: `pointer` for §5.3 forward references and `archived` for §5.2 tombstones. Both are rejected on input.

**Extension.** One field beyond OKF: `seq`, a monotonic integer the server maintains on every write. It gives an agent a cheap way to reason about ordering and staleness across versions without parsing opaque identifiers. It is not the concurrency token — that is the object ETag (§8.4).

**Attribution is not frontmatter.** The user who made each write, and the kind of write it was, are recorded as server-set object metadata (§8.2), never in the frontmatter. Agents rewrite frontmatter freely; they must not be able to rewrite who did what.

**Adopted:** `type` required on every article; `title`, `description`, `resource`, `tags` as recommended keys; the actor convention (`human:<id>`, `<producer>/<version>`, `process:<id>`); the trust family `generated` / `verified` with derived tiers; the lifecycle family `status` / `stale_after`; `sources` for provenance; bundle-relative links. Unknown frontmatter keys MUST be preserved on round-trip rather than dropped.

The trust family earns its place here more than anywhere else: in a system where agents write everything and humans write nothing, *"an agent produced this and nobody has checked it"* versus *"a person confirmed it"* is a distinction readers need constantly.

| Divergence from OKF | Reason |
| --- | --- |
| `index.md` and `log.md` are never stored as tree nodes — generated per request, filtered to the caller's grants | A stored `log.md` would be readable wherever it sat rather than where the history it describes lives, defeating the per-path rule in §5.3; a stored `index.md` would enumerate siblings and break the rule that absence and denial are indistinguishable. OKF permits synthesis, so this stays conformant. |
| Both names reserved in the path grammar | Prevents a stored file shadowing a generated one. |
| Move pointers instead of "tolerate broken links" | OKF's answer to relocation is that consumers must tolerate breakage. A forwarding pointer (§5.3) is strictly stronger and fully compatible. |
| No bundle export in v1 | §1.2 |
| Attested Computation family unused | Built for a different use case; adds surface for no benefit here. |
| Access control and versioning are ours | OKF defines neither. §4 and §5 are extensions, not deviations. |

---

## 4. Authorization

### 4.1 The split

| Concern | Owner |
| --- | --- |
| Client registration (CIMD, DCR) | WorkOS |
| Authorization code flow, PKCE, consent screen | WorkOS |
| Token issuance, refresh rotation, revocation | WorkOS |
| Audience binding to the MCP resource URL | WorkOS, via resource indicators |
| User directory and login methods | WorkOS |
| Protected resource metadata | Substrate |
| Bearer validation — signature, `iss`, `aud`, expiry | Substrate |
| Positional grant decisions (`read` / `write` / `own`) | Substrate |
| Grant-write guard (owners grant only within their subtree) | Substrate |
| Audit log of reads, writes, grants and denials | Substrate |

**WorkOS answers *who is calling*; the substrate owns everything downstream of the token.**

**Migration path.** Because the substrate is a pure resource server, replacing the authorization server later touches exactly three things: the `authorization_servers` entry, the JWKS URL and issuer used in validation, and the resource-indicator registration. No change to the grant model, storage layer, tool surface, or content. One caveat: connector authentication settings cannot be changed after a connector is added, so a switch also means every user removing and re-adding theirs. At four people that is an afternoon.

### 4.2 Normative clauses

These are the contract. Implement them literally.

**AS-1** The MCP server SHALL be an OAuth 2.1 resource server. It SHALL NOT implement `/authorize`, `/token`, `/register`, or any client-registration mechanism.

**AS-2** The substrate SHALL serve Protected Resource Metadata (RFC 9728) at `/.well-known/oauth-protected-resource` **and** at the path-suffixed variant matching its MCP path. The `resource` value SHALL be the canonical MCP URL — lowercase scheme and host, no default port, no trailing slash, no fragment, path included. `authorization_servers` SHALL contain the AuthKit domain as its **first and only** entry; Claude uses the first entry and does not fall back.

**AS-3** On any **unauthenticated** request — no token, or a token that fails AS-4 — the substrate SHALL return **401** with `WWW-Authenticate: Bearer` carrying `resource_metadata` and an explicit minimal `scope`. A 200 carrying an error payload SHALL NOT be used to signal an authentication requirement; Claude does not act on it. A token that is valid but lacks a required scope is a **403** at the HTTP level (§6.5), never a 401.

**AS-4** Every bearer token SHALL be validated on every request against the AuthKit JWKS: signature, `iss` equal to the AuthKit domain, `aud` equal to the canonical MCP resource URL, and expiry. Any failure SHALL be rejected with 401. **The algorithm SHALL be pinned explicitly and never inferred from the token's own header.**

**AS-5** The substrate SHALL NOT forward a received bearer token to any downstream service. Where it calls another service on a user's behalf it SHALL act as a separate OAuth client with a separately issued credential.

**AS-6** Authorization SHALL be evaluated per request against the token's subject. A permission set SHALL NOT be cached for the lifetime of a connection, and the calling identity SHALL NOT be derived from any tool argument or client-supplied parameter. The API Gateway authorizer result cache SHALL be set to **zero**; JWKS is cached in the function, so revalidating every request costs microseconds and leaves AS-6 true without a footnote.

**AS-7** The registered resource indicator in WorkOS SHALL match the `resource` value published in AS-2 byte for byte. Environment-specific URLs SHALL be registered as separate resource indicators.

**AS-8** CIMD SHALL be enabled in the WorkOS dashboard. DCR SHALL remain disabled unless a specific non-Claude client requires it; if enabled, clients SHALL be required to register as public clients with `token_endpoint_auth_method: none`, because AuthKit defaults DCR clients to `client_secret_basic`, which fails token exchange for public clients.

**AS-9** Access token lifetime SHOULD be fifteen minutes or less so a revoked grant stops working promptly. Refresh tokens SHALL be rotated.

**AS-10** The audit log SHALL record, per request: token subject, token identifier, resolved audience, requested path, the authorization decision, and the grants the decision depended on. **Denied reads SHALL be logged**, not only successful ones.

**AS-11** The MCP endpoint and its metadata documents SHALL resolve only to globally routable IPv4 addresses and SHALL be reachable from Anthropic's egress range. Split-horizon DNS, CGNAT, IPv6-only hosts and cross-host redirects on authenticated paths SHALL be treated as unsupported deployment configurations, and stated as such in the tenant prerequisites.

> **AS-6 interacts with the storage design.** §8.5 caches a minted AWS credential per *(subject, prefix, permission)* for its lifetime. A cached credential is a cached permission decision for that prefix, so effective revocation latency is the **longer** of the token lifetime and the credential cache. Both are set to fifteen minutes, which makes them coherent — but **the number must move together in both places** or one silently becomes the real bound.

### 4.3 Resource identity

An instance publishes protected resource metadata under its root and derives its canonical resource URI from it. That URI is the audience of every token the instance accepts.

```
root                https://wiki.famestad.com
MCP endpoint        https://wiki.famestad.com/mcp
resource metadata   https://wiki.famestad.com/.well-known/oauth-protected-resource/mcp
canonical resource  https://wiki.famestad.com/mcp
```

**Fail closed.** Provisioning MUST treat a failed resource registration as a failed install, and the resource server MUST assert that `aud` equals its own canonical URI exactly — never merely that the token verified.

### 4.4 Scopes

Scopes stay coarse and fixed. Path granularity lives in the grant store, never in scope strings — encoding paths into scopes makes the scope space unbounded and pushes the permission model into tokens that cannot be revoked granularly.

| Scope | Admits |
| --- | --- |
| `wiki.read` | Search, list, and read articles — live versions **and the version history of the path** |
| `wiki.write` | Create, update, move, archive, unarchive |

A token's scopes are the **ceiling**. What it can actually touch is the intersection of its scopes and its grants.

### 4.5 Permissions

Three verbs. There is no `history` permission and no `wiki.history` scope.

| Permission | Confers | Implies |
| --- | --- | --- |
| `read` | The live article, and the version history of that path | — |
| `write` | Create, update, move, archive, unarchive | `read` |
| `own` | Every permission the system defines, on that node and everything beneath it — including granting any of them to anyone, `own` included | all of the above |

> **Read covers history, so editing is not redaction.**
>
> Because `read` confers the version history of the path, removing text by writing a new version does **not** hide that text from anyone who can read the path. The prior version still says what it said.
>
> To actually put something out of a reader's reach, **move the article**. A move leaves the prior versions at the vacated path under that path's grants, and the destination begins a fresh chain (§5.3). **The product surface MUST state this wherever people edit or share**, or the model will be misread as redaction.

An owner is the root user of their subtree. There is no permission an owner lacks within it and nothing above them inside it.

Which makes instance administration fall out cleanly: **an admin is the owner of `/`**. Global read, global granting, global delete are not separate powers but consequences of owning the root. What remains genuinely separate is the operational role — creating user accounts, configuring the authorization server, managing connector settings — because those are properties of the instance rather than of the tree, and no tree permission confers them.

### 4.6 Grant semantics

- **Any node.** A grant targets a folder or a single article. Folder grants cascade to everything beneath; article grants apply to one article.
- **Searchable, not listable.** Search is filtered by path against the caller's whole grant set: a folder grant contributes everything beneath it, an article grant contributes that one article (§8.7). So a person granted a single article finds it by searching, the same way they find anything else. `list_folder` on the parent still returns `404`, because the credential minted for an article grant cannot reach the folder's listing (§8.6) and must not — the siblings are not theirs to see.
- **Additive union.** A principal's effective permission on a node is the union of every grant that matches it — inherited from ancestors or attached directly. **There are no deny rules.** Denials over a tree that changes shape are a reliable source of surprise. To carve out an exception, restructure the tree.
- **All grants are positional.** A grant names a place in the tree, not a piece of content — article grants included. Membership follows the path in both directions: an article moved *into* a granted prefix becomes readable by that prefix's grantees, and an article moved *out* of one stops being readable by them.
- **A grant on a vacated path survives as pointer access.** When an article moves away, whoever held a grant on its old path keeps it, and that path now holds a pointer. What the grant buys them is exactly §5.3's disclosure: they learn the article moved and where it went, and can ask. Such grants surface in the admin console as attached to a vacated path, for review rather than silent deletion.

> **A move is a boundary decision.**
>
> Because every grant is positional, moving an article re-evaluates who can read it, in both directions. Neither direction is a side effect to be suppressed — the change in access is very often *why* the move is happening.
>
> So `move_article` MUST compute the full impact before proceeding — who gains access and who loses it. **A move through which anyone *gains* access is refused on the tool surface** (`403 boundary_change`, carrying the report) and is performed in the web application, where a person sees the impact and confirms. Moves that widen nobody's access — within a folder, or narrowing — proceed on the tool surface and return the report. The reasoning is §4.7's: text an agent has read can instruct it, and a move that discloses is the one write that cannot be undone by moving back. Nobody loses access silently either: the pointer left behind tells them it moved and where, which is the prompt to request access at the new location. *(Decided 14 Sep 2026 after review; supersedes "the agent surfaces that to the person".)*

### 4.7 The admin boundary — structural rule

**No operation that creates a principal or changes a grant may be exposed as an MCP tool.**

Agents read content — including content authored elsewhere — and any text an agent ingests is a potential instruction. If `grant_read` is reachable from the tool surface, prompt injection escalates to privilege escalation in one step. Privilege-changing operations live in the web application behind an interactive human session, and nowhere else.

Absent from the tool surface, deliberately: `create_user`, `grant_access`, `revoke_access`, `read_audit_log`, `hard_delete`. **Their absence is a security control, not an omission.**

**Enforced by IAM, not only by omission.** The function that serves the MCP endpoint holds **read-only** permission on the grant table (§8.8). The only role with write permission on grants belongs to the web application. A tool that changed a grant could not be written by mistake, because the role it would run under cannot perform the write.

### 4.8 Where authorization is enforced — the single most important implementation rule

**The storage identity carries exactly the requesting user's permissions — by design, not by discipline.** Not "a privileged credential that we are careful to constrain," but an identity that is *incapable* of reaching what the user cannot reach. Every read is scoped by the authenticated subject at the point the data is fetched, never fetched broadly and filtered afterwards in application code.

The test of whether you have done this correctly: **could a bug in the application layer leak anything?** If yes, the credential is still too strong.

This is what contains agent compromise. An agent reads content, and content can carry instructions; if a filtering bug or an injected instruction bypasses the application layer, a broadly-privileged storage credential turns that into disclosure of the whole tree, while a subject-scoped one turns it into nothing. The documented exfiltrations in this class all share the same root cause — an agent operating with a credential that outranked its user.

Concretely: no service-role or superuser identity on the read path, no query that omits the subject predicate, and **a test that asserts a user with no grants receives an empty result from the storage layer itself rather than from a filter above it.** §8.5 and §8.8 are how this is satisfied by AWS rather than by our own care.

### 4.9 OAuth flow hardening

§4.2 makes the flow work. These make it safe. Each addresses a failure mode observed in the wild on live MCP servers — accepting arbitrary redirect URIs and permitting PKCE downgrade are the common ones.

| Control | Requirement |
| --- | --- |
| Redirect URI matching | Exact string comparison against registered values. No wildcards, no prefix matching, no pattern matching. A changed redirect URI requires re-registration. |
| PKCE | `S256` required, never optional. Reject an authorization request without a code challenge rather than falling back. |
| `state` parameter | Cryptographically random, stored server-side, single-use, deleted after validation, short expiry. Exact match at the callback; missing or mismatched rejects. |
| Consent ordering | The session or cookie carrying `state` is set **only after** the user approves consent — never before. Setting it earlier renders the consent screen bypassable by a crafted authorization request. |
| Session cookies | `__Host-` prefix, `Secure`, `HttpOnly`, `SameSite=Lax`, signed or server-side. Bound to the specific client, not to "this user consented to something once." |
| Consent page framing | `frame-ancestors 'none'` or `X-Frame-Options: DENY`, so consent cannot be clickjacked. |
| Token audience | Never accept a token not issued for this instance, and never forward a received token downstream. |
| CIMD trust policy | The authorization server fetches client metadata from URLs supplied by strangers. Restrict which domains are accepted — for a deployment whose only client is Claude, **an allowlist of that origin removes the entire class of hostile client registrations.** Whether WorkOS offers this is §2 check 6. |

The last row is the cheapest large win available. A general-purpose authorization server must accept any HTTPS client ID; this one does not, because its clients are known.

Most of this table is WorkOS's to implement, not ours — but it is written here because it is what we are relying on them for, and because the consent-screen behaviour in §14 (`O-3`) is still unverified.

### 4.10 Delegation

Owners grant within the subtree they own, at any permission level including `own` itself. This is a statement about *authority*, not about *surface*: owners exercise it in the web application exactly as root-owners do, so §4.7 stands unchanged.

- **Bounded by the tree, not by a counter.** Delegation chains can be arbitrarily long, but they always run down the hierarchy — so the set of people who may grant on a path is exactly the owners of that path and of every ancestor above it. Answering "who can grant here" is a walk up the ancestor chain, bounded by tree depth.
- **Grants outlive their granter.** Every grant records who made it. Removing an owner does not cascade-revoke what they granted; those grants surface in the admin console as unowned, for review rather than silent deletion.

There will be pressure to make granting an agent action — *"share the setup notes with Dana"* is a natural thing to ask. **The safe form is a proposal, not an execution:** an agent may create a *pending* grant request that changes nothing until a human approves it in the web application, on a screen showing what triggered it. Injected text then produces a request nobody acts on, rather than an access change nobody noticed.
---

## 5. Versioning, archival and moves

Every write produces a new immutable version. Nothing is edited in place and nothing is destroyed.

### 5.1 Optimistic concurrency

Several agents may hold the same article in context at once. Every mutating call carries the version token the caller last observed; the write applies only if the article is still at that version, and otherwise fails **without writing** and returns the current state so the caller can merge and retry.

```
update_article(
  path       = "/racing/setup/rear-bar.md",
  content    = "...",
  if_version = "v_8f2c41"        // fails if the article moved on
)
→ 409 { current_version: "v_9a1d07", body: "..." }
```

The token is the S3 object ETag. §8.4 explains why that is a safe identity token on this bucket.

### 5.2 Archive and unarchive

Archiving writes a **tombstone version** — an object with `type: archived`, an incremented `seq`, and the actor recorded — at the top of the path's chain. Unarchiving writes a **restoring version**: the last content version's body and frontmatter, with `seq` incremented again. Both are ordinary entries in that path's history rather than a separate state machine — the path has one continuous version chain, and archival is a thing that happened partway along it. Archive and move are the same mechanism with different payloads (§5.3, §8.3); neither uses an S3 delete marker, because a delete marker carries no actor, no `seq`, and vanishes from history when removed.

An archived article leaves listings and search for everyone, while its prior versions remain reachable to anyone holding `read` on that path. **Creating an article at an archived path is refused** with `409 archived` carrying the tombstone's `version` (§10.9); the caller unarchives, then updates. Nothing continues a chain without the caller saying so.

Hard deletion is an operation for the owner of the subtree **in the web application**, and it is the one action that breaks the immutability guarantee. It should exist, be logged, and be rare.

### 5.3 Move pointers

Paths are identity, and paths move. Rather than carrying a hidden internal id beneath every article, a move leaves a **pointer** at the vacated path recording where the content went. A pointer is a tombstone version with a `moved_to` field — so archiving and moving are one mechanism with different payloads.

This buys something an internal id does not: **references survive.** A cross-instance link captured a year ago still resolves, because the old address answers with a forward reference instead of a 404. Given that references are URL-shaped and travel outside this instance (§7), durability at the old address is worth more than elegance underneath it.

- **Pointers are permanent.** They keep prior versions reachable at the path that holds them, and the pointer sits at the top of that chain — pointing forward to the new location while sitting over the old history. Pruning one orphans everything behind it, so pointers are **never** garbage-collected.
- **The server never follows a chain.** `read_article` on a pointer returns one forward reference and stops. The caller follows it with a fresh call, authorized against the next path. `A→B→A` is reachable through ordinary renaming and is the caller's loop to notice, not the server's to bound — there is no hop limit because there is no server-side traversal to limit.
- **Listings ignore them.** Pointers never appear in `list_folder` or search results; they answer only on direct resolution, so a heavily reorganized tree does not fill with visible debris. The same rule applies to folders: a folder appears in its parent's listing only while it holds at least one visible child (§8.6).
- **History stays behind.** A move begins a fresh version chain at the destination; the article's prior versions remain at the vacated path, underneath the pointer, governed by that path's grants.
- **The pointer is written first.** A move commits at the source before any content lands at the destination (§8.3). A stale `if_version` therefore fails with nothing written, and no content ever crosses a permission boundary for a move that did not complete.

> **History is per path — and with no separate history grant, this is what carries the weight.**
>
> Listing or reading versions at a path returns that path's chain and stops there. The `moved_from` reference recorded at a destination is a link a caller **may follow**, not a traversal the server performs silently — and following it is a separate authorization check against the old path.
>
> The property this preserves: after a move, whoever had access at the old location **keeps the past and loses the present**, while whoever has access at the new location **gets the present and none of the past**. So relocating an article into a shared subtree never discloses what that article used to say. Transparent traversal would be the obvious convenience and would quietly undo this.

> **Pointers disclose the move, not the content.**
>
> A pointer resolves for anyone who could reach the old path and names where the article went. It stops there: no body, no history, no access. Following it into a subtree the reader lacks returns a denial they can act on by asking.
>
> This is safe by construction rather than by policy. A pointer sits at a path *inside the prefix that granted access to the article in the first place*, so anyone able to resolve it already knew the article existed and had read its contents. Concealing the destination would hide nothing they did not have, while misreporting a move as a deletion is the worse failure — it sends people looking for something that is fine.
>
> The residual is the destination *path name*, which can carry information the article never did: `/legal/project-bluebird/` discloses by existing. That is a naming practice rather than a mechanism.

---

## 6. MCP protocol and transport

### 6.1 Transport

Streamable HTTP at `{root}/mcp`. A URL ending `/sse` selects the deprecated SSE transport and MUST NOT be used.

The server implements `server/discover` — mandatory under the 2026-07-28 revision — returning supported protocol versions, capabilities and identity, and negotiates version **per request** rather than through a one-time handshake.

That revision removed protocol-level sessions and the GET stream endpoint: the transport is **POST-only**, and a server may answer each POST with plain `application/json`. Every tool here is fast request-and-response, so **v1 emits no event streams at all.** This removes session state, streaming infrastructure, and the usual objection to running an MCP server on a stateless compute platform.

### 6.2 Protocol conformance

Requirements of the 2026-07-28 revision that are easy to miss because nothing in the AWS stack provides them.

| Requirement | Behaviour |
| --- | --- |
| Mandatory request headers | Every POST carries `MCP-Protocol-Version` and `Mcp-Method`; calls to `tools/call`, `resources/read` and `prompts/get` also carry `Mcp-Name`. Values may arrive base64-sentinel encoded (`=?base64?…?=`) and MUST be decoded before comparison. |
| Header/body agreement | A header that disagrees with the JSON-RPC body is rejected with `400` and error `-32020 HeaderMismatch`. |
| **Origin validation** | **MUST.** A present but invalid `Origin` header is rejected with `403`. This defends against DNS rebinding, and neither API Gateway nor a load balancer does it — it is application code or a WAF rule, and it is the easiest MUST in the specification to forget. |
| Unknown method | `404` with JSON-RPC `-32601`, not `400`. |
| Backward compatibility | `GET` and `DELETE` on the MCP endpoint return `405`. `Mcp-Session-Id` and `Last-Event-ID` are ignored. |
| Discovery challenge | The `401` carries `WWW-Authenticate: Bearer resource_metadata="…"`. A challenge that is present but omits `resource_metadata` is **worse than none** — this constraint rules out some gateway options outright (§9.1). |

### 6.3 Response limits

| Ceiling | Value | Consequence |
| --- | --- | --- |
| Tool result size, Claude apps | ~150,000 characters | Upper bound on any single response |
| Tool result size, Claude Code | 25,000 tokens | **The real budget** — the tightest surface governs |
| Tool call timeout | 240s (300s cited elsewhere) | No synchronous long operations |

Every tool response is therefore bounded by design rather than by accident. `search` returns ranked snippets and paths, never bodies. `read_article` takes an optional section or byte range and reports total size, so a long article is fetched in parts instead of failing at the ceiling. Articles carry a published maximum size (1 MiB), and the admin console warns as one approaches it.

### 6.4 Browser clients and CORS

Claude Desktop and Cowork are not browsers. claude.ai is, but its MCP requests are expected to originate from Anthropic's infrastructure rather than the user's browser — which is why AS-11 requires reachability from Anthropic's egress range. If that holds, no browser ever sends a request to `/mcp` and CORS on it is moot. The configuration below costs ten minutes and is kept as **cheap insurance** rather than as the thing that bites; the thing that bites is AS-11.

- **Preflight succeeds without authentication.** `OPTIONS` on the MCP endpoint is never authorized; a catch-all route with an authorizer attached will swallow it.
- **Allowed headers include the MCP set** — `authorization`, `content-type`, `mcp-protocol-version`, `mcp-method`, `mcp-name`, and `mcp-param-*`.
- **`WWW-Authenticate` is listed in exposed headers**, on ordinary responses and on the `401` gateway response itself (§9.3). A browser cannot read a non-safelisted response header unless the server exposes it.

Where a gateway handles CORS itself it typically **ignores CORS headers returned by the application** — so the exposed-header list must be set in gateway configuration, not in code (§9.4).

### 6.5 Error semantics

| Condition | Level | Response |
| --- | --- | --- |
| No token, or invalid token | HTTP | `401` with `WWW-Authenticate` naming the resource metadata URL |
| Token lacks the required **scope** | **HTTP** | `403` with `WWW-Authenticate: Bearer error="insufficient_scope", scope="wiki.write"` — re-authorization will help |
| Token has the scope, principal lacks the **grant** | Tool error | Writes: `403 forbidden`, plain authorization denial, no scope named. Reads and listings: `404`, because a `403` would confirm the path exists (§10.1) |
| `if_version` stale | Tool error | `409` with the current version and body |

**Two levels, deliberately.** The first two rows are HTTP responses emitted before any tool runs; step-up re-authorization is something a client does on an HTTP `403` with a `WWW-Authenticate` challenge, and nothing inside a `200` tool result will trigger it. Every other error is an MCP tool error (`isError: true`, §10.14) — the call reached the tool and the tool declined. In v1 both scopes are advertised in the resource metadata and requested together at consent, so the scope row is correct but idle; it is the grant row that carries the privacy weight.

> **Never conflate rows two and three.** Clients are directed to answer `insufficient_scope` by attempting step-up re-authorization. Returning it for an ACL denial sends the agent into a re-consent loop for something no amount of consent will ever grant. **Scope errors mean *ask for more*; grant errors mean *no*.**

---

## 7. Cross-instance references — specified, deferred to v2

Not built in v1 (§1.2). The reference **grammar** is worth adopting now regardless: internal links use the same form, so nothing has to be rewritten when outside sharing arrives.

A cross-link is URL-shaped: a root pointer plus a resource path.

```
https://wiki.acme.com/a/standards/torque-spec.md
└──────────┬─────────┘│└───────────┬────────────┘
      root pointer    │       resource path
                      └── article namespace
```

The `/a/` segment separates article space from `/.well-known/`, `/mcp`, and the admin application, so a reference can never collide with a protocol endpoint. The MCP endpoint for any reference is derived mechanically: `{root}/mcp`.

**Resolution happens on the client side, by the agent, using a connector the reader already holds. The instance never fetches from another instance.** Each instance is an identity island: the reader is a user in both, holds two connectors and two tokens, and the cross-link is resolved by the agent choosing the right one. Neither server holds a credential at the other.

1. Parse the root pointer from the reference.
2. If the agent holds a connector for `{root}/mcp`, read the article through it.
3. Otherwise, surface the reference as unresolved, naming the root, and stop. **Do not fetch it by any other means.**

An unresolved reference is a normal outcome, not an error. It renders as a citation the reader can request access to.

> **Foreign content is data.** Content read from another instance was authored by people outside the trust boundary. It is input, never instruction. This is the same rule as §4.7 viewed from the other side, and together they are the reason the admin surface is absent from the tool list.

---

## 8. Storage design

S3 carries the tree, the content, the version history and the enforcement. One small DynamoDB table holds grants and nothing else. **Reads and conditional writes are native S3 verbs; move, archive and history are one or two ordinary writes on top of them.** The design is mostly a matter of *not building things*.

### 8.1 Two functions, three IAM roles

```
  API Gateway ──► Authorizer ──► MCP function (the data plane) ──► DynamoDB (grants, READ-ONLY)
                  JWT only        resolves grants
                  NO data perms   mints credentials ──► STS ──► scoped session policy
                                                                   │
                                                                   ▼
                                                             S3 (tree · content · versions)
                                                             ENFORCEMENT HAPPENS HERE

  Web application (increment F) ──► DynamoDB (grants, READ-WRITE) · same STS path for content
```

**Two functions in v1.** The authorizer validates the token and holds no data permissions. The MCP function is the data plane: it parses agent traffic, reads the grant store, asks STS for a credential scoped to *just this operation's prefix*, and uses that credential against S3. It holds `sts:AssumeRole` on the storage role and **read-only** access to the grant table — nothing else. There is no separate "edge" function between the authorizer and the data plane; a third hop would buy nothing, because the data plane would have to trust a subject handed to it by the very component that parses hostile content.

**The session policy is the boundary.** The credential the data plane holds when it touches an object is **incapable** of reaching beyond the prefix this operation needs. A defect in the function that parses content leaks one prefix, never the tree. **That is §4.8 satisfied by AWS rather than by our own care**, and it is the whole reason for this shape.

**The web application is a third function with the only role that can write grants.** That is what makes §4.7 an IAM property rather than a code-review property.

### 8.2 Bucket layout

One bucket. Versioning enabled, SSE-KMS with a customer-managed key, account-level public access blocking. **Object Lock in production only:** governance mode, one-year default retention (§8.9). Development buckets carry no Object Lock so the account can be torn down.

```
a/<path>.md               articles — the key IS the OKF concept id
a/<path>/_listing.json    per-folder projection (§8.6)
sys/                      system objects, never inside any content grant
```

The `a/` prefix mirrors the article namespace in the reference grammar (§7), so a cross-link's path and its S3 key are the same string with one prefix. Names beginning with `_` are reserved and cannot be created as articles.

Because `_listing.json` lives **inside** the folder it describes, it inherits that folder's grants automatically. No separate permission story for metadata — a credential scoped to `a/racing/*` can read the racing listing and nothing else, for free.

A **pointer** (§5.3) is an ordinary object at the vacated key with `type: pointer` and a `moved_to` field. An **archive tombstone** (§5.2) is an ordinary object at the same key it archives with `type: archived`. Since the bucket is versioned, writing either simply appends a version to that key — so a moved key's chain reads `[content, content, …, pointer]` and an archived key's reads `[content, …, archived]`, with the history underneath, reachable, exactly as required. **Neither is a special mechanism; each is the next version.**

**Every object version carries server-set user metadata**, written on every `PutObject` from the skeleton onward:

```
x-amz-meta-actor        human:<subject>          who made this write
x-amz-meta-kind         write | archive | unarchive | moved_in | moved_out
x-amz-meta-moved-from   <path>                   on moved_in versions only
```

This is where attribution lives. It is not in the frontmatter, because agents rewrite frontmatter and must not be able to rewrite who did what. `ListObjectVersions` does not return user metadata, so `list_versions` reads it with one `HeadObject` per entry (§8.3) — a fan-out accepted for history reads, and one that costs nothing to prepare for by writing the metadata from day one.

### 8.3 Native verbs and write paths

| Tool | S3 operation |
| --- | --- |
| `read_article` | `GetObject`; a `type: pointer` body becomes a forward reference, a `type: archived` body becomes `404` |
| `list_folder` | `GetObject` on `_listing.json`; `ListObjectsV2` with `Prefix`+`Delimiter` plus one `HeadObject` per child as the rebuild source |
| `read_version` | `GetObject?versionId=` |
| `list_versions` | `ListObjectVersions` + one `HeadObject?versionId=` per entry for actor and kind |
| `create_article` | `PutObject` + `If-None-Match: *` |
| `update_article` | `PutObject` + `If-Match: <etag>` |
| `archive_article` | `PutObject` tombstone + `If-Match: <etag>` |
| `unarchive_article` | `GetObject?versionId=<last content>` → `PutObject` restoring version + `If-Match: <tombstone etag>` |
| `resolve_reference` | Nothing — parses a string (§10.6) |
| `move_article` | Four operations, pointer first — see below |
| `search` | Nothing native — §8.7 |

| Operation | Sequence |
| --- | --- |
| `create` | `PutObject If-None-Match: *` → refresh parent `_listing.json` |
| `update` | `PutObject If-Match: <etag>` → refresh listing if projected fields changed |
| `archive` | `PutObject` tombstone `If-Match: <etag>` → refresh listing (child leaves) |
| `unarchive` | `PutObject` restoring version `If-Match: <tombstone etag>` → refresh listing (child returns) |
| `move` | `HeadObject to` (409 if anything is there) → `PutObject` pointer at `from`, `If-Match: <if_version>` → `GetObject from?versionId=<the version beneath the pointer>` → rewrite frontmatter (`seq+1`) → `PutObject to, If-None-Match: *` with `kind: moved_in`, `moved-from` → refresh both listings |

**Why the pointer goes first.** A stale `if_version` fails at the pointer write with **nothing written anywhere**. Content only lands at `to` after `from` has committed to the move. The alternative — copy first, pointer second — can strand a copy at the destination when the pointer write races a concurrent edit, and that copy sits under the *destination's* grants: content crossing a permission boundary for a move that was refused. Pointer-first cannot do that.

**Why a move is not `CopyObject`.** The destination version needs `seq+1`, `moved_from` and the `moved_in` metadata; a byte copy carries none of them. Articles are at most 1 MiB, so a read-rewrite-put is the natural shape anyway.

**One behaviour we get without writing it.** *History survives a move without bookkeeping — and stays where it was.* Because the pointer is just the newest version of the vacated key, `ListObjectVersions` on that key returns the pointer followed by every prior version. No traversal to implement. The destination starts with a single version and inherits no chain. **That storage behaviour is what makes §5.3's per-path history rule enforce itself** — a caller at the new key physically cannot see the old key's versions.

> **S3 has no atomic multi-object operation, of any kind.**
>
> A move is two writes and cannot be made transactional. The mitigation is **idempotence rather than atomicity**. A crash after the pointer write and before the destination write leaves a pointer whose target answers `404` — visibly wrong, but the content is intact one version beneath the pointer and nothing has been disclosed anywhere new. Retrying the same `move_article` call sees the pointer at `from` naming `to`, finds `to` empty, and completes the destination write. `If-None-Match: *` on that write makes a double retry harmless. The window between the two writes is milliseconds absent a crash; during it the article is readable at neither path, which is the correct side to fail on.
>
> This is the one place the rejected DynamoDB-primary draft was genuinely stronger, since `TransactWriteItems` made moves atomic. It is a real cost of this design and is named rather than glossed.

Listing refreshes are not part of any transaction either. `_listing.json` is a cache whose authority is `ListObjectsV2` plus the objects themselves, so a stale or missing listing is a **rebuild**, not a corruption — and its own write is conditional so that concurrent writers cannot lose each other's children (§8.6).

### 8.4 Concurrency

The `if_version` token is the object's **ETag**. A read returns it, a write presents it, and S3 enforces the compare-and-swap: a mismatch returns `412 Precondition Failed` and writes nothing. Version IDs are surfaced separately, in history listings, where their opacity and stability are what matter.

> **Why the ETag is a safe identity token here.**
>
> On an unencrypted or SSE-S3 bucket the ETag is the MD5 of the content, and an article edited A → B → A would return to its original ETag — a stale token from the first A would match the third state. **This bucket is SSE-KMS, and AWS documents that SSE-KMS objects' ETags are not a digest of the plaintext.** Each version's ETag is distinct regardless of content, so `If-Match` is an identity check, not a content check, and the ABA case does not arise.
>
> `seq` in the frontmatter is therefore **not** load-carrying for concurrency. It exists for ordering and staleness reasoning by agents. Do not remove it, and do not rely on it for what the ETag already does.

Every mutating write — update, archive, unarchive, the pointer half of a move — is a conditional `PutObject`, so no write can silently land on top of someone else's edit.

One IAM detail that is easy to miss: **`If-Match` requires `s3:GetObject` in addition to `s3:PutObject`.** A write-only grant cannot perform a conditional write.

### 8.5 Permissions — the grant store and credential minting

S3 enforces; DynamoDB remembers.

| Item | PK | SK | Attributes |
| --- | --- | --- | --- |
| Grant | `U#<subject>` | `<node path>` | `permission` — one of `read`, `write`, `own` — plus `granted_by`, `granted_at` |
| User | `U#<subject>` | `PROFILE` | `email`, `display_name`, `status` |

**GSI1** — `N#<node path>` / `<subject>` — answers "who can reach this node", which the move-impact report and the admin console both need.

The table holds **no content**, so the concern that sank the DynamoDB-primary draft does not arise: a reader of this table learns who may see what, never what the thing says.

**Resolving the cascade.** Effective permission is the union of grants on the node and on every ancestor. The ancestor set falls out of the path string, so this is a **bounded batch point-read, not a scan**:

```
/racing/setup/rear-bar.md
  →  /  ·  /racing  ·  /racing/setup  ·  /racing/setup/rear-bar.md

BatchGetItem(PK="U#<subject>", SK ∈ those four)  →  union
```

Four keys, one round trip, bounded by **depth** rather than by tree size.

**Minting the credential.** Having decided, the data plane calls `sts:AssumeRole` with an inline session policy naming only what *this operation class* touches. There are exactly three policy shapes, defined once in `app/auth/credentials.py`, each with a negative test (§11.3 step 3). This is the **read** shape for a folder grant on `/racing/setup`:

```json
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow",
   "Action":["s3:GetObject","s3:GetObjectVersion"],
   "Resource":["arn:aws:s3:::wiki/a/racing/setup/*"]},
  {"Effect":"Allow",
   "Action":["kms:Decrypt"],
   "Resource":["arn:aws:kms:REGION:ACCOUNT:key/KEY-ID"]}]}
```

| Shape | Adds | Used by |
| --- | --- | --- |
| `read` | `s3:GetObject`, `s3:GetObjectVersion`, `kms:Decrypt` on the exact key (article grant) or prefix (folder grant) | `read_article`, `read_version`, `list_versions`, `search` over article grants |
| `list` | `read` + `s3:ListBucket` on the bucket ARN **with an `s3:prefix` condition** on the folder | `list_folder`, `search` over folder grants, listing rebuilds |
| `write` | `read` + `s3:PutObject`, `kms:GenerateDataKey` on the same resource | every mutating tool; move mints one for each end |

A read credential carries **no** `ListBucket`. `GetObject` does not need it, and an unconditioned `ListBucket` on the bucket ARN is a listing of the whole bucket — the failure is a disclosure, not an error, which is why the `s3:prefix` condition is written here rather than left to be discovered.

Scoping to the **operation** rather than to the user's whole grant set matters twice over: it keeps the policy far inside the 2 KB inline limit no matter how many grants a person accumulates, and a defect in the data plane leaks one prefix rather than everything that user could theoretically reach.

Credentials cache per *(subject, prefix, shape)* for their lifetime — one `AssumeRole` per distinct working area rather than per object. **Cache lifetime must be a configuration value, not a constant, and must move together with token lifetime (AS-6 note).** `AssumeRole` credentials live at least fifteen minutes regardless; the cache can drop one sooner, which is all revocation needs.

### 8.6 Listing index

`ListObjectsV2` returns key, size, ETag, last-modified and storage class — no user metadata, no tags, at any price. A listing that shows titles therefore needs either one `HeadObject` per child or a projection.

The projection is a `_listing.json` object written inside each folder, holding the OKF frontmatter fields for its immediate children. One `GetObject` renders a folder. It is also what the generated `index.md` of §3.4 is built from, and because it lives under the folder's own prefix it needs no permission logic of its own.

```
a/racing/setup/_listing.json
  children: [ {name, type, title, description, tags, status, size, etag} ]
  generated_at
```

Rewritten whenever a child is created, updated in a projected field, archived, unarchived or moved. **Pointers and archive tombstones are excluded** — that is how §5.2's "leaves listings" and §5.3's "listings ignore them" are implemented: the rebuild reads each child's `type` and drops the two reserved values. Authority remains `ListObjectsV2` plus the objects themselves, so a lost or stale listing is repaired by rebuilding it rather than by restoring from backup.

**The listing write is conditional, and tagged.** Every `_listing.json` put carries `Tagging: wiki:listing=true` so the §8.9 lifecycle rule can find it.

**Conditional, because of the race.** Two agents creating articles in one folder at the same moment both read the listing, both add a child, both write — and without a precondition the second write silently drops the first child from every future `list_folder` and `search`, with nothing to detect it. Every listing write therefore carries `If-Match` on the ETag it read; a `412` re-reads and retries, bounded to a handful of attempts. **Rebuild is lazy: a missing listing is rebuilt on first read.** No events, no queues, no second data path — the conditional write closes the race on its own.

**Folder visibility.** A child folder appears in its parent's listing only while it holds at least one visible child of its own. When a folder's last visible child is archived or moved away, the writer refreshing that folder's listing also refreshes the parent to drop the folder; when a child returns, the parent is refreshed to restore it. Folders therefore obey the same no-debris rule as pointers. The `_listing.json` object itself persists, so no folder ever needs re-creating.

*S3 Annotations (June 2026) were considered and do not fit:* `ListObjectAnnotations` is single-object, so it cannot answer a folder query in one call, and annotations are not inherited by new object versions, so every write would have to re-apply them.

### 8.7 Search

S3 has no content search and never has. S3 Select was SQL over structured data inside one object and has been closed to new customers since 2024.

**Search is metadata matching, filtered by path against the caller's grants.** The grant set defines the searchable area; the match runs over titles, descriptions and tags within it. Two kinds of grant, two ways of reaching the metadata:

| Grant | Searchable area | How the metadata is read |
| --- | --- | --- |
| Folder | Everything beneath the prefix | `_listing.json` for the folder, walking child folders from each listing — one small object read per folder |
| Article | That article | A ranged `GetObject` on the key covering the frontmatter — one read per granted article |

Both run under credentials minted for exactly those paths (`list` shape for folders, `read` shape for articles — §8.5). At this scale it is a handful of small reads — AWS's own guidance after retiring S3 Select is client-side filtering. It also has a property no search engine gives cheaply: **only permitted paths are ever read**, so the §4.8 seam closes. A path the caller cannot reach is never fetched, never matched and never ranked; there is no post-filter because nothing outside the grant set enters the candidate set.

The `prefix` argument narrows the area further; it never widens it. A `prefix` outside every grant yields an empty result, indistinguishable from nothing matching.

**When a search engine arrives, the filter survives.** Whatever indexes article bodies must carry the path as a filterable field, and every query must carry the caller's grant set as a path filter — the same rule, applied at the index instead of at S3. That is the one requirement on any future engine, and it is why full-text search is a swap rather than a redesign.

Full-text over article bodies is a later decision and **the tool contract does not change when it arrives.** Two candidates: an external index fed by S3 event notifications, or S3 Vectors for semantic retrieval — the latter offering similarity rather than exact phrase or boolean matching, which is a different product behaviour rather than a cheaper implementation of the same one.

### 8.8 Keys and IAM

- **One customer-managed KMS key** for the bucket and the table. Session policies MUST include `kms:Decrypt` for reads and `kms:GenerateDataKey` for writes; scope both to the one key.
- **Authorizer role:** CloudWatch Logs. No S3, no DynamoDB, no STS, no KMS.
- **MCP (data plane) role:** `dynamodb:GetItem`, `BatchGetItem`, `Query` on the one grant table and its index — **read-only**; `sts:AssumeRole` on the one storage role; **no direct S3 permissions of its own** — every object touch goes through a minted credential.
- **Web application role (increment F):** the same, plus `dynamodb:Scan` (used by `grants_by_granter` — a bounded scan of a table holding tens of rows) and `dynamodb:PutItem`, `UpdateItem`, `DeleteItem` on the grant table. **The only role that can change a grant.**
- **Storage role:** the role assumed per operation. Its own policy is the outer bound (the whole `a/` prefix, the three action shapes of §8.5, the one key); the session policy narrows each credential from there. It never holds `s3:DeleteObjectVersion` or `s3:BypassGovernanceRetention`.
- **Break-glass role:** hard delete and Object Lock bypass. Assumed by a person in the console, never by a function. Logged by CloudTrail.
- **No long-lived access keys anywhere.**
- Account-level public access blocking, and an alarm on any bucket policy change or public grant.

Giving the data plane no S3 permissions of its own is deliberate: it makes the minted credential the **only** path to an object, so §4.8 cannot be bypassed by a code path that forgets to mint one. **There is nothing to forget with.** Giving it no grant-write permission is the same argument applied to §4.7.

### 8.9 Designing for insiders who err

Every user of this system is an insider. Nobody is attacking it, and the permission model exists to prevent accidents and respect boundaries rather than to repel adversaries. But not every insider brings the same judgment — some are learning, some are hurried, and all of them act through an agent that will do what it is asked.

That shifts what the controls are *for*:

| Mechanism | Reads as security | Actually earns its place as |
| --- | --- | --- |
| Immutable versions, archive as a marker | Tamper resistance | **Nothing a person asks for can destroy anything.** A confidently wrong instruction costs one version, not a document. |
| Move-impact report (§4.6) | Disclosure control | A person reorganising a tree is told, before it happens, who stops being able to read what. Reorganisation is exactly where good intentions cause quiet harm. |
| Pending grant requests (§4.10) | Injection containment | An agent can propose sharing; a person decides. The person deciding is the one who understands who they are sharing with. |
| Narrow grants among trusted people | Least privilege | Blast radius of a **mistake**, not of an attack. |
| Ownership separable from writing | Privilege tiering | A person can be trusted to write everywhere they work without being trusted to hand out access. Genuinely different judgments. |

**What this adds:**

- **Object Lock in governance mode, production only, one-year default retention.** For a year, no version can be deleted by any role the application holds, nor by an account-level mistake; after that, IAM is the guard, which it is anyway (§8.8). The storage role never holds the bypass permission; hard delete is a break-glass role assumed by a person in the console. Development buckets are unlocked so the account can be torn down. A lifecycle rule expires non-current listing versions once retention lapses, so the cache does not accumulate forever — lifecycle filters cannot name a file, so listing writes carry the object tag `wiki:listing=true` and the rule keys on that (increment A adds `s3:PutObjectTagging` to the `write` shape and the storage role's outer bound for listing keys only). The shape — locked in prod, unlocked in dev, bypass held by nothing that runs code — is the property; one year is the decided period.
- **Hard delete is owner-only, in the web application, and logged.** It is the single operation that breaks recoverability, so it lives where a person has to go looking for it rather than where an agent can be talked into it.
- **Recovery is a documented procedure, not an improvisation.** Restoring an article is re-putting a prior version; restoring a folder is doing that for each key under a prefix. Written down and rehearsed once.
- **The audit log answers "what happened to my article", not "who betrayed us".** Chronological, per-path, legible to a non-technical person, reachable from the read view rather than buried in an admin console.

> **What this does not change.** The technical controls in §12 stay exactly as they are. An insider-only user population does not make the server less internet-exposed, does not make injected content in an article less dangerous, and does not make a leaked credential less useful to whoever finds it.

### 8.10 Cost and scale

At family scale this is a rounding error: S3 storage for a few megabytes of markdown, a DynamoDB table holding tens of items, Lambda invocations in the low thousands per month. **The largest line is the KMS key.**

Nothing in the layout changes at ten thousand articles. Listings stay one object read per folder, permission resolution stays bounded by tree depth, and version history stays partitioned per key by S3 itself. The two things that would move are search (§8.7) and, if a folder ever held more than a thousand children, listing pagination — which the projection already avoids by being a single object.
---

## 9. Hosting and infrastructure

**API Gateway REST API, regional, with a Lambda REQUEST authorizer in front of a Lambda proxy integration.** Buffered responses; no streaming.

### 9.1 Why REST and not HTTP API

The MCP discovery challenge requires `401` carrying `WWW-Authenticate: Bearer resource_metadata="…"`. **HTTP APIs cannot emit it** — custom gateway responses are a REST-API-only feature, the built-in JWT authorizer emits a fixed header with no `resource_metadata` field, and a Lambda authorizer does not help because an unauthenticated request never reaches it. REST APIs customise gateway responses with a literal header value, which is the whole reason for the choice.

Two secondary reasons: REST integration timeouts are increasable past 29 seconds where HTTP APIs are fixed at 30, and REST's header budget is 20,480 bytes against HTTP's 10,240 — which matters once a bearer JWT shares space with the mandatory MCP header set.

### 9.2 Routes

| Path | Method | Auth | Integration |
| --- | --- | --- | --- |
| `/mcp` | `POST` | Lambda authorizer | Lambda proxy |
| `/mcp` | `GET`, `DELETE` | None | Mock → `405` |
| `/mcp` | `OPTIONS` | None | CORS preflight |
| `/.well-known/oauth-protected-resource` | `GET` | None | Mock, static body |
| `/.well-known/oauth-protected-resource/mcp` | `GET` | None | Mock, static body |

Serving the metadata documents from a **mock integration** means no compute, no cold start, and no possibility of an outage on the path a client uses to discover how to authenticate.

### 9.3 The 401

Customise the API-level `UNAUTHORIZED` gateway response. **The authorizer triggers it by raising, not by returning a Deny policy** — a Deny produces `ACCESS_DENIED` and a `403`, which maps onto the grant-denial case instead and breaks discovery.

```
UNAUTHORIZED (401)
  WWW-Authenticate:                 'Bearer resource_metadata="https://wiki.famestad.com/
                                     .well-known/oauth-protected-resource/mcp", scope="wiki.read"'
  Access-Control-Allow-Origin:      'https://claude.ai'
  Access-Control-Expose-Headers:    'WWW-Authenticate'
```

**The CORS headers go on the gateway response too.** A gateway response bypasses the integration entirely, so the CORS configuration of §9.4 does not apply to it; without these two lines a browser client receives the `401` and cannot read the challenge. This is the invisible failure §16 warns about, and the first draft of this snippet reproduced it.

**Two more gateway responses to customise**, or §12.3's "everything else 404" is false:

```
MISSING_AUTHENTICATION_TOKEN   →  404, empty body    (API Gateway's default for an unknown
                                                       route is a 403 with a misleading message)
DEFAULT_4XX / DEFAULT_5XX      →  generic body, no stack, no request echo
```

### 9.4 CORS

Configure at the gateway, **not** in application code — where a gateway handles CORS it ignores headers the integration returns.

```
allowOrigins  https://claude.ai
allowMethods  POST, OPTIONS
allowHeaders  authorization, content-type, mcp-protocol-version,
              mcp-method, mcp-name, mcp-param-*
exposeHeaders WWW-Authenticate     ← without this, discovery fails
                                     invisibly in the browser
```

### 9.5 Custom domain

Regional endpoint, ACM certificate in the same region, base path mapping of `(none)` so both `/mcp` and `/.well-known/…` serve from one API. The protected resource metadata must come from the resource's own origin, which this arrangement gives for free.

### 9.6 Environments

**Separate AWS accounts for development and production. Not separate stacks in one account.**

The reasoning is §4.8's: the strongest guarantee that a development mistake cannot touch family records is that the development credentials are *incapable* of reaching them. Stack separation relies on nobody pointing the wrong environment variable at the wrong bucket, which is a thing people do at eleven at night.

Each environment is a complete instance with its own domain, bucket, table, key and — the part that surprises people — **its own canonical resource URI and its own registered resource at the authorization server** (AS-7). A development instance whose resource registration silently failed would issue tokens against a default audience, and the resulting confusion would be blamed on anything but that.

```
prod   https://wiki.famestad.com/mcp
dev    https://wiki-dev.famestad.com/mcp
```

**Development data is synthetic.** Family records never enter the development account — not for debugging, not temporarily, not "just this once to reproduce something."

---

## 10. Tool contract

Eleven tools. The `$defs` block is the data model in schema form — everything the server stores appears there first.

| Tool | Arguments | Scope | Permission |
| --- | --- | --- | --- |
| `search` | `query, prefix?, limit?` | `wiki.read` | `read` on each hit |
| `list_folder` | `path` | `wiki.read` | `read` on path |
| `read_article` | `path, section?, byte_range?` | `wiki.read` | `read` |
| `resolve_reference` | `url` | — | — |
| `list_versions` | `path, limit?, cursor?` | `wiki.read` | `read` on path |
| `read_version` | `path, version_id, byte_range?` | `wiki.read` | `read` on path |
| `create_article` | `path, content, frontmatter` | `wiki.write` | `write` on any ancestor |
| `update_article` | `path, content, if_version, frontmatter?` | `wiki.write` | `write` |
| `move_article` | `from, to, if_version` | `wiki.write` | `write` on both; refused with `boundary_change` when anyone would gain access (§4.6) |
| `archive_article` | `path, if_version` | `wiki.write` | `write` |
| `unarchive_article` | `path, if_version?` | `wiki.write` | `write` |

**The retrieval layer is replaceable.** Every read is a search or a targeted fetch — an agent must never need to walk the tree to find something — so §8.7 can be swapped without changing this table.

### 10.1 Conventions

**Identity.** An article's path is its identity — absolute, lowercase, `.md`-suffixed. Strip the suffix and you have its OKF concept id. The schema enforces lowercase; the server never normalizes (§3.1).

**Concurrency.** `version` is the object's ETag: opaque, returned by every read, required by every mutating call as `if_version`. Compare verbatim after stripping surrounding quotes; **never parse it**. The separate `seq` integer in frontmatter is for ordering and staleness reasoning, never for concurrency (§8.4).

**Pointers.** A read that lands on a pointer returns one `forward_reference` and stops. The caller follows it with a fresh call. The server never walks a chain (§5.3).

**Scopes.** `wiki.read` and `wiki.write`. Reading a path's version history falls under `wiki.read` and needs no separate permission — history lives at the path it belongs to, and the grant on that path governs it.

**Bounded responses.** Two signals appear consistently: `truncated` (boolean, on anything list-shaped or body-bearing) and `cursor` (opaque, present only when more remains). Bodies additionally carry `total_bytes` and `returned_bytes`.

**Invisibility.** Listings and search return only what the caller may see. A path the caller lacks `read` on does not appear; it is never returned with a denial marker. **Absence and denial are indistinguishable from outside.** Search covers every grant the caller holds, folder or article; listings cover folder grants only (§4.6).

> **Ship the definitions inlined.** MCP clients do not resolve `$ref` across documents. The `$defs` block below is written once for readability and **MUST be inlined into every tool schema that references it** before publication.

### 10.2 Shared definitions

#### Paths and tokens

```json
"article_path": {
  "type": "string",
  "pattern": "^/(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*\\.md$",
  "maxLength": 512,
  "description": "Absolute article path, lowercase, '.md' suffixed. Example
    '/racing/setup/rear-bar.md'. Segments may not begin with '_' (reserved for
    system objects) and the names 'index.md' and 'log.md' are reserved."
},
"folder_path": {
  "type": "string",
  "pattern": "^/$|^/(?:[a-z0-9][a-z0-9._-]*)(?:/[a-z0-9][a-z0-9._-]*)*$",
  "maxLength": 512,
  "description": "Absolute folder path with no trailing slash; '/' is the root."
},
"version": {
  "type": "string", "maxLength": 256,
  "description": "Opaque concurrency token (the object ETag). Compare verbatim;
    never parse. Pass back unchanged as if_version."
},
"version_id": {
  "type": "string", "maxLength": 1024,
  "description": "Opaque identifier for one historical version. Used only to pin
    a read; not valid as if_version."
},
"actor": {
  "type": "string", "maxLength": 256,
  "pattern": "^(human:[A-Za-z0-9._@-]+|process:[A-Za-z0-9._-]+|[^/]+/[^/]+)$",
  "description": "OKF actor convention: 'human:<id>' for a person, 'process:<id>'
    for an automated process, '<producer>/<version>' for an agent."
}
```

#### Frontmatter

```json
"frontmatter": {
  "type": "object",
  "required": ["type"],
  "additionalProperties": true,
  "properties": {
    "type":        { "type": "string",
                     "description": "OKF concept type. Starting vocabulary is
                       'doc'. 'pointer' and 'archived' are server-written and
                       rejected on input." },
    "title":       { "type": "string", "maxLength": 200 },
    "description": { "type": "string", "maxLength": 500,
                     "description": "One sentence. Shown in listings and search." },
    "tags":        { "type": "array", "items": { "type": "string", "maxLength": 60 },
                     "maxItems": 20 },
    "status":      { "enum": ["draft", "stable", "deprecated"], "default": "stable" },
    "stale_after": { "type": "string", "format": "date-time" },
    "sources":     { "type": "array", "items": { "$ref": "#/$defs/source" } },
    "generated":   { "type": "object", "required": ["by"],
                     "properties": { "by": { "$ref": "#/$defs/actor" },
                                     "at": { "type": "string", "format": "date-time" } } },
    "verified":    { "type": "array", "items": {
                     "type": "object", "required": ["by"],
                     "properties": { "by": { "$ref": "#/$defs/actor" },
                                     "at": { "type": "string", "format": "date-time" } } } },
    "seq":         { "type": "integer", "minimum": 1, "readOnly": true,
                     "description": "Server-maintained. Increments on every write.
                       Rejected on create; ignored on update, where an agent handing
                       back a block it read will naturally include it." }
  }
},
"source": {
  "type": "object", "required": ["resource"],
  "properties": {
    "resource": { "type": "string", "maxLength": 2048 },
    "id":       { "type": "string", "maxLength": 64 },
    "title":    { "type": "string", "maxLength": 200 },
    "author":   { "type": "string", "maxLength": 200 }
  }
},
"trust": {
  "enum": ["unverified", "machine-confirmed", "human-reviewed"],
  "description": "Derived, never stored. No 'verified' entries is unverified;
    entries by non-human actors only is machine-confirmed; any 'human:' actor
    makes it human-reviewed."
}
```

#### Results

```json
"article_summary": {
  "type": "object",
  "required": ["path", "type", "version", "trust"],
  "properties": {
    "path":        { "$ref": "#/$defs/article_path" },
    "type":        { "type": "string" },
    "title":       { "type": "string" },
    "description": { "type": "string" },
    "tags":        { "type": "array", "items": { "type": "string" } },
    "status":      { "enum": ["draft", "stable", "deprecated"] },
    "stale":       { "type": "boolean", "description": "True when stale_after has passed." },
    "trust":       { "$ref": "#/$defs/trust" },
    "size_bytes":  { "type": "integer" },
    "seq":         { "type": "integer" },
    "version":     { "$ref": "#/$defs/version" }
  }
},
"version_entry": {
  "type": "object",
  "required": ["version_id", "seq", "kind", "actor", "at"],
  "properties": {
    "version_id": { "$ref": "#/$defs/version_id" },
    "seq":        { "type": "integer" },
    "kind":       { "enum": ["write", "archive", "unarchive", "moved_in", "moved_out"] },
    "actor":      { "$ref": "#/$defs/actor" },
    "at":         { "type": "string", "format": "date-time" },
    "size_bytes": { "type": "integer" },
    "moved_to":   { "$ref": "#/$defs/article_path" },
    "moved_from": { "$ref": "#/$defs/article_path" }
  }
},
"forward_reference": {
  "type": "object",
  "required": ["kind", "moved_to"],
  "properties": {
    "kind":     { "const": "forward_reference" },
    "moved_to": { "type": "string", "maxLength": 2048,
                  "description": "Destination path, or an absolute URL when the
                    destination is on another instance." },
    "moved_at": { "type": "string", "format": "date-time" },
    "note":     { "type": "string",
                  "description": "Human-readable, e.g. 'This article moved. You may
                    not have access at its new location — ask an owner.'" }
  },
  "description": "Returned in place of content when a path holds a pointer. Naming
    the destination is deliberate and safe: only a caller who could reach the old
    path can see this, and they could already read the article."
},
"access_change": {
  "type": "object",
  "required": ["subject", "direction", "permission"],
  "properties": {
    "subject":      { "type": "string" },
    "display_name": { "type": "string" },
    "direction":    { "enum": ["gains", "loses"] },
    "permission":   { "enum": ["read", "write", "own"] },
    "via":          { "type": "string",
                      "description": "The granted path the change derives from." }
  }
}
```

### 10.3 `search` — `wiki.read` · `read` on each hit

Find articles by words in their title, description or tags, within everything the caller can see — folder grants contribute their subtrees, article grants contribute themselves. Returns ranked summaries with a snippet — **never article bodies**. Use this first when you do not already know a path.

```json
// input
{ "type": "object", "required": ["query"],
  "properties": {
    "query":  { "type": "string", "minLength": 1, "maxLength": 400 },
    "prefix": { "$ref": "#/$defs/folder_path",
                "description": "Restrict to this folder and everything beneath it." },
    "limit":  { "type": "integer", "minimum": 1, "maximum": 50, "default": 10 } } }

// output
{ "type": "object", "required": ["hits", "truncated"],
  "properties": {
    "hits": { "type": "array", "items": {
      "allOf": [ { "$ref": "#/$defs/article_summary" } ],
      "properties": {
        "snippet": { "type": "string", "maxLength": 400 },
        "score":   { "type": "number" } } } },
    "truncated": { "type": "boolean" } } }
```

**Errors:** none at the tool level. An empty `hits` array is a valid result and does not distinguish "nothing matched" from "nothing you may see".

### 10.4 `list_folder` — `wiki.read` · `read` on path

Immediate contents of one folder — child folders by name, articles as summaries. Does not recurse. Archived articles and move pointers never appear.

```json
// input
{ "type": "object", "required": ["path"],
  "properties": { "path": { "$ref": "#/$defs/folder_path" } } }

// output
{ "type": "object", "required": ["path", "folders", "articles", "truncated"],
  "properties": {
    "path":      { "$ref": "#/$defs/folder_path" },
    "folders":   { "type": "array", "items": { "$ref": "#/$defs/folder_path" } },
    "articles":  { "type": "array", "items": { "$ref": "#/$defs/article_summary" } },
    "truncated": { "type": "boolean" } } }
```

**Errors:** `404` when no such folder exists. **A folder the caller cannot read returns `404`, not `403`**, to preserve indistinguishability.

### 10.5 `read_article` — `wiki.read` · `read`

Current live version of one article: frontmatter plus body. Returns a forward reference instead of content when the path holds a move pointer — **one hop**; the server does not follow it, the caller does, and the next read is authorized against the next path.

```json
// input
{ "type": "object", "required": ["path"],
  "properties": {
    "path":    { "$ref": "#/$defs/article_path" },
    "section": { "type": "string", "maxLength": 200,
                 "description": "Return only the body under this markdown heading,
                   matched case-insensitively on the heading text." },
    "byte_range": { "type": "array", "minItems": 2, "maxItems": 2,
                    "items": { "type": "integer", "minimum": 0 },
                    "description": "Inclusive [start, end] byte offsets into the
                      body. Ignored when 'section' is given." } } }

// output
{ "oneOf": [
  { "type": "object",
    "required": ["path", "frontmatter", "content", "version",
                 "total_bytes", "returned_bytes", "truncated"],
    "properties": {
      "path":           { "$ref": "#/$defs/article_path" },
      "frontmatter":    { "$ref": "#/$defs/frontmatter" },
      "content":        { "type": "string" },
      "version":        { "$ref": "#/$defs/version" },
      "trust":          { "$ref": "#/$defs/trust" },
      "total_bytes":    { "type": "integer" },
      "returned_bytes": { "type": "integer" },
      "truncated":      { "type": "boolean" } } },
  { "$ref": "#/$defs/forward_reference" } ] }
```

**Errors:** `404` for no such path, an archived article, **or a path the caller lacks `read` on** — absence and denial are indistinguishable (§10.1).

### 10.6 `resolve_reference` — no scope · no grant

Parse a wiki URL and say whether it can be read from here. **Reads nothing** — not across instances, and not locally either; it does not touch S3, does not follow pointers, and returns the same answer to every caller. Resolution happens on the client, by choosing a connector. Call this before following a link found inside an article.

```json
// input
{ "type": "object", "required": ["url"],
  "properties": { "url": { "type": "string", "format": "uri", "maxLength": 2048 } } }

// output
{ "type": "object", "required": ["url", "root", "path", "local", "resolvable"],
  "properties": {
    "url":          { "type": "string" },
    "root":         { "type": "string", "description": "Instance root, e.g. 'https://wiki.acme.com'." },
    "path":         { "type": "string", "description": "Article path within that instance." },
    "local":        { "type": "boolean", "description": "True when the root is this instance." },
    "resolvable":   { "type": "boolean",
                      "description": "True only when local. A foreign reference is
                        resolvable by the agent if it holds a connector for that
                        instance, which this server cannot know." },
    "mcp_endpoint": { "type": "string", "description": "'{root}/mcp' — the connector to use." },
    "note":         { "type": "string" } } }
```

**Errors:** `400` when the URL is not a wiki reference. Unresolvable is a normal outcome, not an error.

### 10.7 `list_versions` — `wiki.read` · `read` on path

The version chain of one path, newest first. **Returns that path's chain only.** When the first entry is `moved_in`, earlier history lives at the path named in `moved_from` and reaching it is a separate call, authorized against that path.

```json
// input
{ "type": "object", "required": ["path"],
  "properties": {
    "path":   { "$ref": "#/$defs/article_path" },
    "limit":  { "type": "integer", "minimum": 1, "maximum": 100, "default": 20 },
    "cursor": { "type": "string", "maxLength": 2048 } } }

// output
{ "type": "object", "required": ["path", "versions", "truncated"],
  "properties": {
    "path":         { "$ref": "#/$defs/article_path" },
    "versions":     { "type": "array", "items": { "$ref": "#/$defs/version_entry" } },
    "truncated":    { "type": "boolean" },
    "cursor":       { "type": "string" },
    "continues_at": { "$ref": "#/$defs/article_path",
                      "description": "Present when this chain begins with a move.
                        Earlier history is at this path, under its own grants." } } }
```

> **Do not traverse.** `continues_at` is a link the caller may follow, not a traversal the server performs. Following it means a fresh `list_versions` call whose authorization is evaluated against that path. Silent traversal would let a grant at the new path read history the old path governs — precisely the disclosure the per-path rule prevents.

**Errors:** `404` for no such path or one the caller lacks `read` on.

### 10.8 `read_version` — `wiki.read` · `read` on path

One historical version, pinned by its `version_id`. Same shape as `read_article` but immutable and never a forward reference.

```json
// input
{ "type": "object", "required": ["path", "version_id"],
  "properties": {
    "path":       { "$ref": "#/$defs/article_path" },
    "version_id": { "$ref": "#/$defs/version_id" },
    "byte_range": { "type": "array", "minItems": 2, "maxItems": 2,
                    "items": { "type": "integer", "minimum": 0 } } } }

// output
{ "type": "object",
  "required": ["path", "version_id", "frontmatter", "content",
               "actor", "at", "total_bytes", "returned_bytes", "truncated"],
  "properties": {
    "path":           { "$ref": "#/$defs/article_path" },
    "version_id":     { "$ref": "#/$defs/version_id" },
    "seq":            { "type": "integer" },
    "frontmatter":    { "$ref": "#/$defs/frontmatter" },
    "content":        { "type": "string" },
    "actor":          { "$ref": "#/$defs/actor" },
    "at":             { "type": "string", "format": "date-time" },
    "total_bytes":    { "type": "integer" },
    "returned_bytes": { "type": "integer" },
    "truncated":      { "type": "boolean" } } }
```

**Errors:** as `list_versions`, plus `404` when the version does not belong to that path.

### 10.9 `create_article` — `wiki.write` · `write` on any ancestor

Create a new article. **Fails if anything already occupies the path, including a move pointer or an archive tombstone** — a vacated path is retired permanently, and an archived one is restored with `unarchive_article` rather than overwritten. Ancestor folders are created implicitly; since folders are not objects, the permission check is `write` on any ancestor of the new path, which the grant cascade (§8.5) answers directly.

```json
// input
{ "type": "object", "required": ["path", "content", "frontmatter"],
  "properties": {
    "path":        { "$ref": "#/$defs/article_path" },
    "content":     { "type": "string", "maxLength": 1048576,
                     "description": "Markdown body without frontmatter." },
    "frontmatter": { "$ref": "#/$defs/frontmatter" } } }

// output
{ "type": "object", "required": ["path", "version", "seq"],
  "properties": {
    "path":    { "$ref": "#/$defs/article_path" },
    "version": { "$ref": "#/$defs/version" },
    "seq":     { "type": "integer" } } }
```

**Errors:** `403` without `write` on an ancestor; `409` when the path is occupied, with `code` of `exists`, `archived` or `retired_pointer` — the `archived` case carries `current_version` (the tombstone's ETag) so the caller can unarchive without a second round trip; `400` for a reserved name, a caller-supplied `seq`, or a reserved `type`.

### 10.10 `update_article` — `wiki.write` · `write`

Replace the body, and optionally the frontmatter, of an existing article. Requires the `version` from your most recent read; a mismatch writes nothing and returns the current state.

```json
// input
{ "type": "object", "required": ["path", "content", "if_version"],
  "properties": {
    "path":        { "$ref": "#/$defs/article_path" },
    "content":     { "type": "string", "maxLength": 1048576 },
    "if_version":  { "$ref": "#/$defs/version" },
    "frontmatter": { "$ref": "#/$defs/frontmatter",
                     "description": "Omit to leave frontmatter unchanged. When given
                       it replaces the previous block entirely, except for
                       server-maintained fields." } } }

// output
{ "type": "object", "required": ["path", "version", "seq"],
  "properties": {
    "path":    { "$ref": "#/$defs/article_path" },
    "version": { "$ref": "#/$defs/version" },
    "seq":     { "type": "integer" } } }
```

**Errors:** `403` without `write`; `404` for no such path; `409` on a stale `if_version`, carrying `current_version` and the current body up to the response cap.

### 10.11 `move_article` — `wiki.write` · `write` on both

Relocate an article, leaving a permanent forward pointer at the old path. **Returns who gains and who loses access.** Because grants are positional, a move re-evaluates readership in both directions — surface `access_changes` to the person before treating the move as done.

```json
// input
{ "type": "object", "required": ["from", "to", "if_version"],
  "properties": {
    "from":       { "$ref": "#/$defs/article_path" },
    "to":         { "$ref": "#/$defs/article_path" },
    "if_version": { "$ref": "#/$defs/version" } } }

// output
{ "type": "object",
  "required": ["from", "to", "version", "seq", "access_changes"],
  "properties": {
    "from":    { "$ref": "#/$defs/article_path" },
    "to":      { "$ref": "#/$defs/article_path" },
    "version": { "$ref": "#/$defs/version",
                 "description": "The article's version at its new path." },
    "seq":     { "type": "integer" },
    "access_changes": {
      "type": "array", "items": { "$ref": "#/$defs/access_change" },
      "description": "Empty when the move crosses no permission boundary." },
    "history_note": { "type": "string",
      "description": "'Earlier versions remain at {from} and are not readable from
        the new location.'" } } }
```

**Errors:** `403` without `write` on either end; `404` for no such source; `409` on a stale `if_version` or an occupied destination. **A `409` means nothing was written** — the pointer is the first write and it is conditional (§8.3). Retrying the identical call after a transport failure is safe and completes a half-finished move.

### 10.12 `archive_article` — `wiki.write` · `write`

Remove an article from listings and search. Nothing is destroyed — prior versions stay reachable to anyone who can read that path.

```json
// input
{ "type": "object", "required": ["path", "if_version"],
  "properties": {
    "path":       { "$ref": "#/$defs/article_path" },
    "if_version": { "$ref": "#/$defs/version" } } }

// output
{ "type": "object", "required": ["path", "version", "seq", "archived"],
  "properties": {
    "path":     { "$ref": "#/$defs/article_path" },
    "version":  { "$ref": "#/$defs/version" },
    "seq":      { "type": "integer" },
    "archived": { "const": true } } }
```

**Errors:** `403` without `write`; `404` for no such path or already archived; `409` on a stale `if_version`.

### 10.13 `unarchive_article` — `wiki.write` · `write`

Restore an archived article to listings. Writes a restoring version — the last content version's body and frontmatter, `seq` incremented — so the chain reads `[…, content, archived, content]` and both events are visible in history.

```json
// input
{ "type": "object", "required": ["path"],
  "properties": {
    "path":       { "$ref": "#/$defs/article_path" },
    "if_version": { "$ref": "#/$defs/version",
                    "description": "Optional. The tombstone's version, as returned by
                      list_versions or by the 409 from a create_article attempt on this
                      path. Supply it to guard against a concurrent restore." } } }

// output
{ "type": "object", "required": ["path", "version", "seq", "archived"],
  "properties": {
    "path":     { "$ref": "#/$defs/article_path" },
    "version":  { "$ref": "#/$defs/version" },
    "seq":      { "type": "integer" },
    "archived": { "const": false } } }
```

**Errors:** `403` without `write`; `404` when the path was never used or is not archived.

### 10.14 Error envelope

Errors are returned as MCP tool errors (`isError: true`) with `structuredContent` matching the shape below. The only one an agent is expected to act on programmatically is `409`, which carries enough to merge and retry without a second round trip.

**Not in this envelope:** authentication and scope failures. Those are HTTP `401` and `403` responses emitted before any tool runs (§6.5), because step-up re-authorization is something a client does on an HTTP challenge and never on a tool result.

```json
"error": {
  "type": "object", "required": ["status", "code", "message"],
  "properties": {
    "status":  { "enum": [400, 403, 404, 409, 429, 500] },
    "code":    { "enum": ["bad_request", "forbidden", "boundary_change", "not_found",
                          "conflict", "rate_limited", "exists", "archived",
                          "retired_pointer", "internal"] },
    "message": { "type": "string",
                 "description": "Written for a person. Says what to do next where
                   there is something to do." },
    "current_version": { "$ref": "#/$defs/version" },
    "current_body":    { "type": "string" },
    "retry_after":     { "type": "integer" } } }
```

| Status | Meaning | Does retrying help? |
| --- | --- | --- |
| `400` | Malformed input, reserved name, read-only field supplied | Not without changing the request |
| `403` | Principal lacks the grant | **No.** Ask an owner |
| `403` + `boundary_change` | The move would give someone access | **No.** Do it in the web application, where the person confirms the impact (`access_changes` is in the envelope) |
| `404` | No such path — or one the caller may not see | No |
| `409` | Stale `if_version`, or an occupied destination | Yes, after merging |
| `429` | Rate limited | After `retry_after` |
| `500` | Storage or minting failure; body says nothing more | Once, then stop |

### 10.15 Judgment calls already made

Decided here rather than left open. Each is cheap to reverse before implementation and expensive after.

| Call | Reasoning |
| --- | --- |
| Unreadable paths return `404`, not `403` — every read tool | Preserves the rule that absence and denial are indistinguishable. A `403` would confirm the path exists. Writes still return `403`: a caller attempting a write has already named the path. |
| Paths cap at 512 characters | A `list` session policy carries the folder path twice and must stay under STS's 2 KB inline limit; 512 leaves headroom for any bucket name. Nobody has a 512-character path. |
| Paths are lowercase, rejected rather than normalized | Case-sensitive paths over a case-insensitive-ish client surface produce articles that differ only in capitalisation. A schema rejection is visible to the agent; a silent rewrite is not. |
| Article bodies cap at 1 MiB | Far above anything a person writes, far below anything that troubles the storage layer, and it makes "too large to read" a write-time error rather than a read-time surprise. |
| `frontmatter` replaces rather than merges on update | Merge semantics make removing a tag impossible to express. Omit the field to leave it alone. |
| Archive is a written tombstone, not a delete marker | A delete marker has no actor and no `seq`, and removing it on unarchive erases the archive from history. One mechanism for archive and move; every event attributable. Cost: listings filter by `type` rather than getting it free from `ListObjectsV2`. |
| Create at an archived path is refused | Continuing a chain should be a stated intention (`unarchive_article`), not a side effect of a create. The `409` carries the tombstone's version so it costs no extra call. |
| `if_version` optional on unarchive | The tombstone's version is available from `list_versions` or the create `409`; supplying it guards a concurrent restore. Not required, because the common case is one person restoring one thing. |
| Move writes the pointer first | A stale `if_version` then fails with nothing written, and no content reaches the destination's grants for a move that did not complete. Cost: a crash between the writes leaves the article briefly unreadable at both paths rather than readable at both — the correct side to fail on. |
| One hop per read; no server-side chain walk | Every hop is a fresh authorization against the next path, which is §5.3's property with nothing to get wrong. Removes hop limits and cycle detection from the server entirely. |
| Article grants are searchable, not listable | Search filters by path against the whole grant set, so a granted article is found the same way as anything else. The parent folder's listing stays out of reach because the siblings in it are not the grantee's to see. |
| Actor lives in object metadata, not frontmatter | Agents rewrite frontmatter freely. Attribution must be something they cannot rewrite. |
| Scope errors are HTTP; everything else is a tool error | Step-up re-authorization triggers on an HTTP challenge. A tool result cannot start it. |
| `resolve_reference` needs no scope | It parses a string and reports whether this instance is the target. It reads no content and reveals nothing about the tree — a foreign URL returns the same answer to everyone. |
| Search matches metadata, not bodies | Matches v1's listing-based implementation. Full-text is a later swap that widens what matches without changing this contract. |
| `move_article` carries `history_note` | The per-path history rule surprises people exactly once. Saying it in the response is cheaper than a support conversation. |
---

## 11. Stack, repository and build order

### 11.1 Stack

| Layer | Choice | Note |
| --- | --- | --- |
| Backend runtime | Python 3.13 on Lambda | ARM64. `uv` + `pyproject.toml`; a `Makefile` wraps every command |
| MCP | MCP Python SDK | Stateless mode; no session handling needed under 2026-07-28. **Confirm the SDK implements that revision before step 7** — `server/discover`, the `Mcp-*` headers, `-32020`. If it does not, `server.py` hand-rolls the transport (a few hundred lines) and the SDK is used for schemas only. Decide this at step 7, not by discovery halfway through it |
| Token validation | `PyJWT` + `cryptography` | In the authorizer only. Pin algorithm, issuer, audience, expiry explicitly |
| AWS | `boto3` | STS, S3, DynamoDB |
| Web application | Python, server-rendered (Jinja2), on a third Lambda at `/app/*` of the same API | **Decided at increment F, overriding the earlier TypeScript note.** One language across the codebase; same-origin cookies so no CORS or token-in-browser; the smallest surface to review. Markdown rendered with HTML disabled (article bodies are untrusted). A static front end can replace it later without touching the data path. |
| Infrastructure | CDK in Python | One language across app and infra |
| Tests | `pytest` | Security checks are tests, not a checklist — §11.3 step 9 |

> **The four lines that matter most in the whole codebase.** Every JWT decode pins all four explicitly. Never infer the algorithm from the token's own header — a mainstream framework shipped exactly that as its default in January 2026.
>
> ```python
> jwt.decode(token,
>            key=jwks_key,
>            algorithms=["RS256"],        # never from the token header
>            issuer=AUTHKIT_DOMAIN,
>            audience=CANONICAL_MCP_URL)  # AS-4
> ```

### 11.2 Repository

```
wiki-substrate/
├── pyproject.toml            uv; one workspace for app and infra
├── Makefile                  deploy · test · security · synth
├── infra/                    CDK app (Python)
│   ├── app.py
│   └── stacks/
│       ├── storage.py        bucket (Object Lock prod-only), table, KMS key
│       ├── api.py            REST API, routes, gateway responses, CORS, domain
│       └── compute.py        two functions — authorizer, MCP/data plane — and their roles
├── app/
│   ├── authorizer/           JWT validation. No AWS data permissions.
│   ├── mcp/
│   │   ├── server.py         transport, headers, origin, server/discover
│   │   └── tools/            one module per tool
│   ├── auth/
│   │   ├── grants.py         ancestor resolution
│   │   └── credentials.py    the three session-policy shapes; the only AssumeRole caller
│   └── storage/
│       ├── articles.py       S3 reads and conditional writes; sets actor/kind metadata
│       └── listings.py       _listing.json projection, conditional write (increment A)
├── tests/
│   ├── unit/
│   └── security/             §12.8 checks, automated; run against the dev account
└── docs/                     this handoff
```

**`app/storage/` is the only package importing `boto3` for S3. `app/auth/credentials.py` is the only place that calls `AssumeRole`.** Both are enforced by IAM as well as by convention — the data plane role holds no S3 permissions of its own, so a module that forgets to mint a credential simply cannot read anything. `tests/security/` are integration tests: they need a deployed development instance and run in CI against it, not on a laptop with mocks.

### 11.3 Build order

Each step is independently testable and proves something the next one depends on. **Do not reorder** — the sequence exists so that a failure tells you exactly what broke.

**Step 1 — Storage stack.**
Bucket with versioning, SSE-KMS with a customer-managed key, account-level public access blocking. **No Object Lock in the development account** (§8.2) — it arrives with the production stack at increment G. Grant table with its index. The storage role with its outer-bound policy, and nothing else that can touch S3.
*Proves:* nothing yet. It is the substrate everything else sits on.

**Step 2 — Grant resolution, offline.**
`app/auth/grants.py` with unit tests only. Given a subject and a path, compute the ancestor set and return the effective permission.
*Proves:* the permission model is correct before anything can depend on it being correct. **Test the empty case hardest** — a subject with no grants must resolve to nothing.

**Step 3 — Credential minting, offline.**
`app/auth/credentials.py`: the three session-policy shapes of §8.5, each as a function, each with a negative test. Mint a `read` for `/racing/`, then attempt `/private/x.md` — confirm `AccessDenied` **from AWS** rather than from your code. Mint a `list` for `/racing/` and attempt `ListObjectsV2` with no prefix — confirm it is refused, not that it returns the whole bucket. Mint a `read` and attempt `ListBucket` at all — confirm refusal.
*Proves:* §4.8 — and **the negative cases prove it properly.** The `list` case is the one whose failure mode is a disclosure rather than an error, which is why it is a test and not a note.

**Step 4 — WorkOS configuration.**
Staging environment, CIMD enabled, the dev resource indicator registered. **Run all seven §2 checks before writing the authorizer** — `scripts/oauth_gate.py` does the four that need a browser.
*Proves:* the authorization server behaves as assumed. If check 4 fails, stop and switch providers — everything downstream is unaffected. If 5, 6 or 7 fail, note the consequence in §14 and continue.

**Step 5 — Authorizer.**
Lambda REQUEST authorizer validating signature, issuer, audience and expiry, result cache TTL **zero** (AS-6). Passes `sub` and `scope` to the integration as context. **Three rejection tests before anything else:** wrong audience, `alg: none`, expired.
*Proves:* tokens are actually checked. Write the rejection tests first; a validator that accepts everything passes every happy-path test.

**Step 6 — API stack and metadata.**
REST API, routes, the customised `401` **with its CORS headers**, `MISSING_AUTHENTICATION_TOKEN` → `404`, generic `DEFAULT_4XX`/`5XX`, CORS on `/mcp`, custom domain, gateway throttle (§12.6) and a billing alarm. The two well-known documents from mock integrations.
*Proves:* discovery works. `curl` the endpoint with no token and confirm the `WWW-Authenticate` header carries `resource_metadata` and the response carries `Access-Control-Expose-Headers`. `curl` a nonsense path and confirm `404`.

**Step 7 — MCP transport.**
First, confirm what the SDK provides for the 2026-07-28 revision (§11.1) and decide how much of `server.py` is yours. Then: `server/discover`, the mandatory header checks with `-32020` on mismatch, origin validation, `405` on GET and DELETE, `404` with `-32601` for unknown methods. Scope check ahead of dispatch, returning HTTP `403` with the `insufficient_scope` challenge (§6.5).
*Proves:* protocol conformance independent of any tool. A client can connect and list zero tools successfully.

**Step 8 — Four tools.**
`create_article`, `read_article`, `list_folder`, `update_article` against the §10 schemas. Conditional writes on create and update. **Every `PutObject` sets the actor and kind metadata of §8.2 from this step onward** — history is increment C, but the attribution it reads must already be there. `read_article` recognises `type: pointer` and `type: archived` bodies now, even though nothing writes them yet; it is four lines and it means increment B changes no read path.
*Proves:* the skeleton. Connect from Claude Desktop and write something.

**Step 9 — Security tests.**
Automate §12.8 as `tests/security/`. The route table. Zero-grant user sees nothing and **no credential was minted** for the request. Credential cannot escape its prefix; `list` credential cannot escape its `s3:prefix`.
*Proves:* that it stays proved. **This is the highest value-per-hour work in the project and the easiest to skip.** Write these before the skeleton is "done", not after — every one is a check you would otherwise perform by hand once, at the moment you are most eager to move on, and never again.

### 11.4 The skeleton: scope and definition of done

**One goal: prove the authentication chain end to end before anything is built on it.** Every architectural risk in this project lives in the path from a Claude connector to a scoped AWS credential. Everything else is ordinary application code.

**In scope:** one user authenticating through WorkOS from Claude Desktop · one grant row (`own` on `/`) · four tools · the full credential path (token validated at the gateway, subject to the data plane, grant resolved, STS session policy minted, S3 touched with that credential and no other) · protected resource metadata, the `401` challenge, and `server/discover`.

**Explicitly out:** no web application · no search · no move, archive, unarchive, or history · no listing projection (`list_folder` reads `ListObjectsV2` directly and returns paths without titles) · no second user · no production account.

> **Done looks like:** you add `https://wiki-dev.famestad.com/mcp` as a custom connector in Claude Desktop, it completes CIMD registration and consent without you pasting anything, and you ask Claude to write an article and read it back. **Then you decode the access token by hand and confirm its `aud` is exactly your canonical MCP URL.**
>
> That last step is the point of the whole exercise. Everything after it is easier.

### 11.5 Increments after the skeleton

| | Increment | Brings |
| --- | --- | --- |
| `A` ✓ | Listing projection and search | `_listing.json` written conditionally on every mutation, lazy rebuild, folder-visibility propagation; `search` walks projections under folder grants and reads frontmatter for article grants, filtered by path against the grant set. Titles appear in listings for the first time. |
| `B` ✓ | Move, archive, unarchive | Pointer-first move, archive tombstones, restoring versions, the access-impact report. The first operations that touch two keys — hence after the security tests exist. |
| `C` ✓ | History | `list_versions` (with the per-entry `HeadObject`) and `read_version` under `wiki.read`, and the per-path rule with no silent traversal. |
| `D` ✓ | Real grants | Grants beyond the hardcoded row, the grant-write guard, the audit log, per-subject rate limits. **Nothing here is an MCP tool**, and nothing here runs under the MCP function's role. |
| `E` ✓ | Second user | Invitation, magic link, connector instructions. The first time the permission model does real work. |
| `F` ✓ | Web application | Read view and admin console. The largest single build and the surface your family judges it by. |
| `G` ◐ | Production | Second AWS account, production WorkOS environment, its own resource indicator, the full pre-launch checklist. |

**Increment E is the first honest test of the product.** Everything before it works whether or not the permission model is right, because there is only one person.

**Build state, 13 September 2026.** A–F are built and unit-tested; G's code (ops stack, CI, runbook, prod config) is built, and what remains of G is operational: the second AWS account, the production WorkOS environment, DNS, the restore rehearsal, and running `tests/security/` against a live instance. `docs/DEPLOY.md` is the sequence. The only things ever proven against real AWS or real WorkOS are the ones the §2 gate and `make aws-tests` prove — neither has run yet.

### 11.6 The web application (increment F)

Three jobs, all of them human.

- **Authentication and consent.** The authorization code flow requires a browser. Even a wiki nobody edits by hand needs login and consent screens.
- **Read view.** Browsable, link-navigable articles for people who would rather look something up than ask an agent, with the version history of whatever they can read.
- **Administration.** User creation, grants and revocations, ownership assignment, audit log, hard delete — everything §4.7 keeps off the tool surface.

Since non-technical people must be able to operate this, **the admin console is the surface that decides whether they can.** Grants should read as sentences — *"Dana can read everything under Racing"* — not as ACL rows. Owners, not just root-owners, live here too: §4.10 makes every owner an administrator of their own subtree, so the console must scope itself to what the viewer owns rather than assuming a single all-seeing operator.

**How the web application authenticates (decided).** It is its own confidential OAuth client of AuthKit — authorization code + PKCE, `openid profile email` — and proves the login with the **ID token**, verified with the algorithm pinned exactly as the authorizer does. The access token is discarded: the app never calls anything on the user's behalf; it acts under its own role with the user's grants, resolving them per request and minting credentials per operation, the same data path as the tools (§4.8). The session is a signed `__Host-` cookie (`Secure; HttpOnly; SameSite=Lax`, 12 h); revocation is checked per request against the PROFILE status, so disabling a person takes effect on their next click, not at cookie expiry (§12.9). Every state-changing form carries a CSRF token bound to the session. A WorkOS identity with no PROFILE row is refused at login — default deny (§3.3) applies to the web app exactly as to the tools.

**Hard delete (decided).** No function holds `s3:DeleteObjectVersion` (§8.8), so the console does not delete. It renders the break-glass runbook for a path — the exact version ids and the commands — and logs that it was viewed. The deletion is performed by a person under the break-glass role in the console. "Exists, logged, rare" is satisfied; "reachable from a function" deliberately is not.

**Adding a person (decided, §15.2 amended; revised 14 Sep 2026).** The web application holds **no WorkOS management API key.** That key can change any user's email address — an identity takeover of every family member if the function or the secret were compromised — and it would be exercised perhaps four times in the instance's life. Instead the operator creates the person in the WorkOS dashboard (which sends the sign-in invitation); the console's "Add a person" takes the resulting user id and email, writes the PROFILE row and the initial grant, and shows the owner the connector block to send on. The only WorkOS credential any function holds is the web application's own OAuth client secret, which can do nothing but exchange that client's login codes.

**Signing out everywhere (decided 14 Sep 2026).** Each PROFILE row carries a `session_epoch`; a session cookie records the epoch it was issued under, and the per-request PROFILE read (already made for the disabled check) rejects a mismatch. "Sign out everywhere" bumps the epoch. A copied cookie dies the moment its owner asks, not at the 12-hour expiry.

**Pending grant requests (§4.10) are deferred past v1.** Granting stays human-only either way; the agent-proposal path can arrive later without touching the tool surface's security posture.

---

## 12. Security

### 12.1 Threat model

Naming who we are defending against matters more than listing controls, because it decides which controls are worth their cost.

| Adversary | What they do | Defended? |
| --- | --- | --- |
| Automated internet-wide scanners | Continuously probe every public address for open databases, missing auth, exposed `.env` files, known CVEs. Not targeting us; targeting everyone. | **Primary threat.** Everything in §12.3 exists for this. |
| Opportunistic attacker following a scan hit | Finds an unauthenticated endpoint or a public snapshot, pulls the data, sometimes extorts. | **Yes** — §12.2 rows 1–4 |
| Malicious content reaching an agent | Text in an article, or from a linked instance, carrying instructions that make the agent exfiltrate other content. | **Yes** — §4.7, §4.8, §7 |
| A compromised user account | Someone's Claude account or email is taken over; the attacker inherits exactly their grants. | **Partly.** Blast radius is bounded by that user's grants — the argument for granting narrowly even among people you trust. |
| A targeted attacker who wants *this* data specifically | Spends real effort on us in particular. | **No.** Out of scope, and honestly so. |
| A malicious insider | An authorized person discloses what they were legitimately shown. | **No.** Not a technical problem. |

### 12.2 Design-level controls

| Risk | Control | § |
| --- | --- | --- |
| Agent compromise reaching the whole tree | Storage identity scoped to the requesting subject; never a superuser read path | 4.8, 8.5 |
| Prompt injection escalating to privilege change | No principal or grant operations in the tool surface | 4.7 |
| Cross-organization token replay | Audience validated against this instance's exact canonical URI | 4.3 |
| Silent loss of isolation via default audience | Provisioning fails closed; install-time negative test | 2, 15.1 |
| Destructive agent action | Immutable versions; archive is a tombstone, not a delete; no agent tool destroys data | 5.2 |
| Unintended access change by relocation | Moves report who gains and who loses; the pointer notifies the displaced | 4.6 |
| Untrusted content from foreign instances | Treated as data; no server-side fetch | 7 |
| SSRF through client metadata fetch | Egress allowlisting and redirect validation on CIMD retrieval (WorkOS's, plus the CIMD trust policy) | 4.9 |
| Over-broad delegation by a subtree owner | Grants record their granter; unowned grants surface for review | 4.10 |
| Re-consent loops from misused error codes | Scope errors and grant errors kept distinct | 6.5 |

### 12.3 Operator posture

The design-level controls above are the interesting half. **The boring half is what actually causes disclosures, and it is all operational.**

- **Authentication terminates in front of the application.** A managed authorizer at the gateway, so no request reaches application code without a validated token. Measurement of internet-exposed MCP servers keeps finding a large share with no authentication at all, and hand-written OAuth in the rest is where the flaws cluster. The lesson is not "be careful" — it is "do not put this in your own code."
- **Token validation pins everything explicitly.** Algorithm, issuer, audience, expiry — never inferred from the token's own header.
- **No secrets in files.** Function roles for cloud access, a managed secret store for the rest (the WorkOS API key is the only one in v1), repository push protection on. Nothing typed into a `.env`, nothing in a deployment package, no tokens in logs.
- **The data store answers only to this account's roles.** S3 and DynamoDB are public-endpoint services; there is no network to hide them in. The controls are: account-level public access block; a bucket policy and a table resource policy that deny every principal not in this account; no IAM user, anywhere, with a policy naming either. "Reachable from the internet" is true of every S3 bucket and is not the property that matters — "answers to nothing but the storage role's session credentials" is.
- **Least-privilege compute roles.** Resource-level permissions to exactly the storage this instance uses, no wildcards, no long-lived access keys anywhere. The MCP function cannot write a grant; the storage role cannot delete a version (§8.8).
- **Patching is automated, not intended.** A managed runtime with no host to patch, dependency updates on auto-merge, and a scheduled rebuild-and-deploy on a fixed cadence whether or not anything changed, so a patched runtime and patched dependencies reach production without anyone deciding to act. Time from public vulnerability to mass exploitation is now measured in weeks, and a solo maintainer's manual patch cadence is, in practice, zero.
- **Errors say nothing.** Debug off in production, generic `4xx`/`5xx` bodies at the gateway (§9.3), one path prefix exposed at the edge and everything else `404` before it reaches the application.

### 12.4 Repository and build practices

**Set these up before the first commit, not after the first incident.** Committed credentials are the most common failure in this whole section, and a leaked credential stays valid until someone rotates it — leaking is automatic, rotating is not.

- **Push protection and secret scanning on, from the first commit.** A credential-bearing commit is rejected at push time rather than discovered later. This is the control that matters most, because it acts **before** the secret is public rather than after.
- **No secret is ever a file.** Cloud access through function roles; everything else from a managed secret store, fetched at runtime.
- **AI-assisted commits get the same scrutiny, and then some.** Agents paste what they were shown, including things that were shown to them by mistake. Given how this system will be built, push protection is not belt-and-braces — it is the primary control.
- **Dependencies pinned to exact versions**, with automated update PRs and auto-merge for patch and minor releases.
- **Scheduled rebuilds.** Dependencies are re-resolved and the functions redeployed on a fixed cadence whether or not anything changed, so a patched dependency reaches production without anyone deciding to act.
- **Branch protection on the default branch**, with review required. The supply-chain incidents of the last two years share a pattern: a change nobody looked at, merged into something many people ran.

### 12.5 Backups are a second copy of everything

> **The most commonly overlooked disclosure path.** A backup holds everything the permission model protects, with **none of the permission model attached**. Snapshots marked public while debugging, exports written to storage without public-access blocking, and unencrypted local copies are a recurring and well-documented source of private-data exposure — including the most-cited credential breach of recent years, where the primary store was never touched and the attackers took the backups.

Requirements: account-level public-access blocking rather than per-container · encryption with a customer-managed key so a leaked copy is inert · an automated check that alarms on any snapshot shared beyond this account · **a restore rehearsed at least once** so the backup is known to work before it is needed.

### 12.6 Rate limiting

The realistic failure here is not abuse. It is an agent in a retry loop, or a well-meaning person asking for something that fans out into four hundred calls. Limits exist to make that cheap and visible rather than to repel anyone.

| Bound | Starting value | Where | When |
| --- | --- | --- | --- |
| Gateway throttle | 20 req/s steady, 50 burst | API Gateway stage | **Day one** (step 6) |
| Billing alarm | Daily spend above a small fixed number | CloudWatch | **Day one** (step 6) |
| Per-subject calls | 60/minute | Data plane, counter row in the grant table | Increment D |
| Per-subject writes | 200/hour | Data plane, same row | Increment D |
| Response | `429` with `Retry-After` | Every rejection logged with the subject and the tool | With each |

The gateway throttle is the bound that protects the bill; at 20 req/s a runaway loop costs single-digit dollars a day and the alarm makes it visible the same day. Per-subject limits cannot be done at the gateway — usage plans key on API keys, not token subjects — so they are application code, and they land with the audit log because both need the same per-subject rows. These are starting values, not findings. Watch the logs for a fortnight and move them.

### 12.7 Observability

One structured line per tool call, in JSON:

```
timestamp · request_id · subject · token_id · tool · path · decision · grants_used · duration_ms · bytes
```

`decision` is `allow` or `deny`; `grants_used` names the grant rows the decision depended on, as `(node, permission)` pairs. This is AS-10's audit record — one line per call, denials included.

> **What must never reach a log:** access tokens, refresh tokens, the `Authorization` header in any form, session cookies, article bodies, and the values of frontmatter fields. **Paths and titles are fine; content is not.** Tokens in logs are among the most reliably exploited findings in the industry, and the mechanism is always the same — someone logged the whole request object while debugging and never took it out.

**Metrics:** call count, latency and error rate per tool; counts of `401`, `403`, `409` and `429`; credential-minting latency.

**Alarms:** any `5xx`; an authentication-failure spike; any change to the bucket policy or public-access configuration; any snapshot shared outside the account; listing-rebuild failures.

**Retention:** ninety days. **Two logs, deliberately.** The application log above records every tool call, reads and denials included — that is AS-10 and it is not optional. CloudTrail S3 data events are enabled for **write operations only**: they answer "what touched the bucket outside the application", which only writes can answer, and read events at S3 level would cost more than they reveal. (Whether CloudTrail read events are ever wanted is the §14 item; the application log is settled.)

### 12.8 Pre-launch checklist

**Every item is verifiable by running something, not by believing something.** Automate as `tests/security/` (build step 9).

1. **The route table.** With no `Authorization` header, every route answers exactly as listed and nothing else answers at all:

   | Route | Expected |
   | --- | --- |
   | `GET /.well-known/oauth-protected-resource` and `…/mcp` | `200`, the metadata document |
   | `OPTIONS /mcp` | `204`, CORS headers, no auth |
   | `GET /mcp`, `DELETE /mcp` | `405` |
   | `POST /mcp` | `401` with `WWW-Authenticate` carrying `resource_metadata`, plus `Access-Control-Expose-Headers` |
   | Anything else — `/`, `/health`, `/metrics`, `/admin`, a random string | `404`, empty body |

2. Present a token minted for a different audience; confirm rejection. Present one with `alg: none`; confirm rejection. Present an expired one; confirm rejection.
3. Request a token carrying this instance's `resource` parameter, decode it, confirm `aud` matches the canonical URI exactly. Request one for an unregistered URI and confirm refusal rather than a silent default audience.
4. **Storage answers to nothing but the storage role.** Anonymous `GetObject` on a known key: `AccessDenied`. `GetObject` from a role in another account: `AccessDenied`. `GetItem` on the grant table from any principal other than the two application roles: `AccessDenied`. Confirm no IAM user in the account holds a policy naming the bucket or the table.
5. Create a user with zero grants. Confirm search, listing and direct reads all return empty — **and confirm that no `AssumeRole` call was made for any of those requests** (§4.8). A zero-grant user has no prefix to mint for; the assertion is that nothing was minted, not that a filter ran. The positive half of §4.8 is step 3's prefix-escape tests, which stay in this suite.
6. Scan the repository and the deployment package for credentials. Confirm none.
7. Confirm account-level public-access blocking is on, backups are encrypted with the customer-managed key, and **a restore has actually been performed**.
8. Trigger a server error in production configuration. Confirm the response body reveals nothing.

### 12.9 Two named, accepted risks

> **Revocation is prospective.** Revoking a grant stops future reads. It does not remove content already placed in someone's context, conversation history, or notes. The product should say this plainly wherever access is granted, rather than implying a recall that does not exist.

> **Public exposure is a product requirement, not a choice.** Using this from a phone and from Cowork requires the server to be reachable from the public internet — a hosted AI client cannot connect to anything else. §12.3 is the price of that requirement rather than optional hardening.
---

## 13. Decision register

Recorded so none of these come back looking attractive in six months. Each was evaluated **against this design**, not in the abstract. If you want to reopen one, the burden is showing that the reason below no longer holds.

### 13.1 Authorization server — rejected

| Rejected | Because |
| --- | --- |
| **Amazon Cognito, directly** | Supports neither DCR nor CIMD, so Claude cannot obtain a client identity against it. Its OIDC discovery omits `code_challenge_methods_supported`, which the specification says obliges clients to refuse to proceed. It does not advertise `none` among token endpoint auth methods, so CIMD would never be selected even if supported. Serves neither RFC 9728 nor RFC 8414 metadata, and exact-matches redirect URIs, which breaks Claude Code's ephemeral loopback port. **Decisively:** AWS documents that revoked user-pool tokens *"will still be valid if they are verified using any JWT library that verifies the signature and expiration of the token."* For a store whose entire premise is per-user authorization, revocation that does not revoke is disqualifying. |
| Cognito behind a passthrough OAuth proxy | Reproduces every precondition of the confused-deputy attack: a static upstream client, dynamically identified downstream clients, an upstream session cookie that suppresses consent, and no per-client consent unless built. Inherits the revocation defect and Cognito's five-minute minimum token lifetime, and needs a consent screen built anyway — Cognito's managed login is a sign-in UI with no consent semantics. |
| **Cognito behind a shim that *is* the authorization server** | **Deferred, not rejected.** The shim owns `/authorize` and `/token`, mints its own correctly-audienced tokens, and demotes Cognito to upstream OIDC login. Sound, and the right design *if the substrate must own its authorization server*. Preserves the per-tenant story better than any hosted option. Costs three to five weeks of security-critical work. **The leading candidate for the productized configuration.** |
| DCR bridge provisioning a Cognito app client per registration | One thousand app clients per pool, **ten thousand as an unadjustable maximum**, `CreateUserPoolClient` limited to five requests per second — and Claude registers a new client on every fresh connection. The ceiling cannot be raised past. |
| Bedrock AgentCore Gateway | Not an authorization server. Invokes Lambda targets with **its own service role** and a context object containing no subject, no token and no claims; the `Authorization` header cannot be allowlisted — so the substrate would be blind to who is calling and could not mint per-user credentials at all. Origin validation (a MUST) could not be confirmed or added. Its protected resource metadata returns the gateway's own domain, which AWS documents as a known gap. |
| Bedrock AgentCore Runtime | Does hand the end user's token to the code it hosts and passes MCP through untouched. But its endpoint is a region-scoped encoded-ARN URL with no custom domain, it provides neither DCR nor CIMD, and it does not replace the authorization server. Reaching `wiki.famestad.com/mcp` means a CloudFront layer anyway — at which point it buys container hosting, not auth. |
| Keycloak | Its own documentation states resource indicators are unsupported and that it therefore only partially supports every MCP revision from 2025-06-18 onward. CIMD experimental. Operational burden high for four users. |
| Auth0 | Proprietary `audience` wins over standard `resource` when both are present. |
| Clerk | **Viable, not selected.** CIMD in beta behind a support request; resource indicators undocumented. Its client trust-policy UI is the best in the field and worth revisiting. |
| Stytch Connected Apps | **Viable, not selected.** Purpose-built for this, CIMD since October 2025, comparable free tier. Acquired by Twilio November 2025 — a longevity consideration rather than an objection. **The substitute if WorkOS fails the §2 gate.** |
| Logto self-hosted | **Retained as the product-phase hedge.** MPL-2.0 and ships inside a customer's own deployment, which fits self-deployment better than any hosted service. Open-source-vs-cloud parity for CIMD and resource indicators unverified (§14 `O-2`). |

### 13.2 Storage and model — rejected

| Rejected | Because |
| --- | --- |
| DynamoDB as the primary store | S3 gives versioning, concurrency and enforcement natively. DynamoDB retains grants only. **Cost accepted:** moves lose atomicity (§8.3). |
| S3 delete markers for archive | No actor, no `seq`, and unarchive-by-removing-the-marker erases the archive from history. A written tombstone gives archive the same shape as a move and every event an author. **Cost accepted:** listings filter by `type` instead of getting it free from `ListObjectsV2`. |
| Copy-first move | A stale `if_version` detected at the second write strands a copy under the destination's grants for a move that was refused. Pointer-first cannot leak, and a retry completes a half-finished move. **Cost accepted:** during a crash window the article is briefly unreadable at both paths. |
| Server-side pointer-chain resolution | Every hop would need its own authorization inside one call, plus hop limits and cycle detection. One hop per read gives the caller the same result with nothing for the server to get wrong. |
| A third "edge" function between authorizer and data plane | The data plane would have to trust a subject handed to it by the component that parses hostile content, which is the trust problem it was meant to remove. Two functions; the session policy is the boundary. |
| Attribution in frontmatter | Agents rewrite frontmatter. Server-set object metadata is the only place an agent cannot reach. **Cost accepted:** one `HeadObject` per entry in `list_versions`. |
| S3 Access Grants | Grantees must be IAM principals or Identity Center directory users, so an external OAuth subject requires standing up Identity Center, a trusted token issuer, and SCIM sync into a second directory. No filtered listing, so the tree-walking layer gets built regardless. Deleting a grant does not invalidate credentials already issued — **a floor of fifteen minutes under any revocation**. Self-minted session policies give the same containment with revocation bounded by our own TTL. |
| Internal resource ids beneath the tree | Move pointers keep old references resolving; an id leaves a 404 at the old address. References are URL-shaped and travel outside this instance, so durability at the old address is worth more than elegance underneath it. |
| Grants that follow content through a move | Creates a channel where access survives a deliberate restriction. All grants are positional. |
| Deny rules | Denials over a tree that changes shape surprise people. Restructure instead. |
| **A relationship-tuple authorization engine (Zanzibar / OpenFGA)** | The case for it is that a prefix grant re-evaluated on move is one tuple rewrite against a non-atomic rewrite of every grant row under a prefix. **That premise does not hold here:** grants are *positional*, so a move mutates **no grants at all** — the article changes position and the existing grants simply describe it differently. The proposal also concedes the two things that would matter most: the tuple store does not authorize its own writes, so the owner-delegation invariant must still be enforced in our own audited code path; and cached decisions can outlive a revocation, requiring a consistency token per document. Both costs survive; the benefit does not. |
| **A separate `history` permission** | History is keyed by path and does not follow content through a move, so the old path's grants already govern the old versions — the asymmetry a history grant was protecting is provided by the storage model itself (§8.3). Read covers the history of the path. **Cost accepted:** a read grant exposes text removed from the live article; removal means moving the article, not editing it (§4.5). |
| Storing `index.md` and `log.md` | A stored `log.md` would be readable wherever it sat rather than where the history it describes lives, defeating the per-path rule. Generated, filtered, never stored. |
| S3 Annotations for listings | `ListObjectAnnotations` is single-object and annotations are not inherited by new object versions. |
| S3 Select for search | Closed to new customers since 2024; it was SQL over structured data inside one object, never content search. |

### 13.3 Scope — deferred

| Deferred | Because |
| --- | --- |
| Unattended agents | Claude connectors do not support machine-to-machine flows at all. When this returns it is an authenticated HTTP API beside the MCP endpoint, not another connector. |
| Users outside the organization | Removes invitation onboarding, cross-instance resolution, the managed-org gap, and untrusted foreign content — at no cost to v1. |
| Bundle export | An export is a complete copy carrying none of the permissions that protected it. Needs scoping and disclosure logging first. |
| Event streams | 2026-07-28 made SSE optional and removed sessions. Every tool here is fast request-and-response. |
| Full-text search | Nothing native in S3. Listing projections answer metadata search at this scale; **the tool contract does not change when an engine arrives.** |

### 13.4 Buy-versus-build — confirmed

Twenty-nine products evaluated. Only Outline clears the remote-MCP-with-per-user-OAuth bar, and its licence forbids offering it as a service to third parties, which rules it out for the product. Positional grants with owner delegation eliminate the rest of the field. **Build stands.**

### 13.5 Consequences of the WorkOS decision, stated honestly

**Positive.** The largest and most security-critical component of the build disappears — no token issuance, no consent UI, no CIMD fetcher and its SSRF hardening, no refresh rotation, no client store. Setup is about a day rather than a sprint. Cost is zero at family scale. Revocation, audience binding and PKCE are correct by construction rather than by our implementation. (Note: **no Python library implements server-side CIMD today** — the MCP Python SDK issue has been open since December 2025 and FastMCP's was closed without shipping. On this stack, building it means hand-writing the fetcher.)

**Negative — identity is now hosted.** A third party holds the user directory and issues the tokens. For a household this is an ordinary trade, but it is a dependency the substrate did not previously have, and an availability dependency on the authorization path.

**Negative — the product-phase tension is real and unresolved.** "Each tenant deploys their own backend" and "our authorization server is a hosted SaaS" are in conflict. Either every customer signs up for their own WorkOS account as a deployment prerequisite, or one WorkOS account with Organizations serves all — at which point the deployment is no longer tenant-owned. **Deliberately deferred so v1 can ship** (§14 `O-1`).

**Negative — cosmetic identity at login.** Without a custom domain (US$99/month, production environments only), users authenticate at a generated `*.authkit.app` hostname. Given that non-technical people must use this, a small trust cost rather than a functional one.

---

## 14. Open items

None of these block the skeleton. Tracked so they are not lost.

| Item | Bearing | Decide by |
| --- | --- | --- |
| Credential cache lifetime | Shorter means faster revocation and more `AssumeRole` calls. Fifteen minutes to start; must be a config value, and must move together with token lifetime (AS-6). | Build step 3 |
| Permanent retirement of moved-from paths | Pointers are permanent, so a vacated path can never host a new article. Confirm this is acceptable, or pointers need an expiry story. | Increment B |
| CloudTrail S3 read events | The application log already records every read (AS-10, §12.7). This is only whether bucket-level read events are wanted on top, at their cost. | Increment D |

*Retired by the post-review decisions:* the `s3:prefix` condition (now written into §8.5 and tested at step 3), the listing rebuild trigger (lazy, conditional write — §8.6), the pointer chain depth (no server-side traversal — §5.3), the Object Lock retention period (one year — §8.9), and article-grant discoverability (searchable by path filter, not listable — §4.6, §8.7).
| `O-3` AuthKit consent screen behaviour | What identity it shows for CIMD clients, whether it displays the `client_id` URL host rather than the self-asserted `client_name`, and whether it warns on loopback redirect URIs. **This is the anti-phishing surface**, now WorkOS's to get right rather than ours. | Before increment E |
| `O-4` Measured revocation latency | End to end, including the credential cache. Confirm token lifetime and refresh configurability against AS-9. | Before increment E |
| `O-5` Anthropic's published egress range | A hardcoded CIDR in a deployment prerequisite is exactly the kind of fact that goes stale without anyone noticing. Verify before AS-11 is treated as normative. | Increment G |
| `O-2` Logto open-source parity | For CIMD and RFC 8707, before treating it as the product-phase hedge. | Post-v1 |
| `O-1` Authorization server for the productized configuration | Each customer provisions their own WorkOS account; or the product abandons self-deployment for a hosted multi-tenant offering; or it ships the shim-as-authorization-server design with Cognito or Logto behind it. An investment and go-to-market question as much as an architectural one. | Post-v1 |
| Read access for users who cannot add a connector | §15.3. Deferred with outside users, but it decides whether the web app ever needs a signed-in read-only path for people who are not agent users. | v2 |
| S3 Files | Exposes a bucket as NFS with real POSIX rename, which would solve §8.3's non-atomic move. Its interaction with versioning is undocumented. Worth ten minutes before it is ruled in or out. | Opportunistic |

---

## 15. Deployment and onboarding

### 15.1 What installation involves

1. Deploy the backend; point a domain at it.
2. Configure the authorization server and register this instance's canonical resource URI. **This step MUST fail the install if it fails** (§4.3).
3. Create the first user and make them the owner of `/`.
4. **Verify:** request a token carrying the `resource` parameter, decode it, confirm `aud` is exactly this instance's canonical URI. Then request one for an unregistered URI and confirm it is **refused** rather than silently issued against a default audience.

**WorkOS setup, concretely:**

1. Create a WorkOS account. No payment method required; a staging environment is provisioned immediately.
2. Record the AuthKit domain (`https://<generated>.authkit.app`) — this is the token issuer.
3. Connect → Configuration: enable **CIMD**. Leave DCR off unless a non-Claude client requires it (AS-8).
4. Connect → Configuration: add the canonical MCP URL as a **resource indicator** and set it as the default for clients omitting `resource`. Register staging and production **separately** (AS-7).
5. Implement AS-2 and AS-3 on the MCP server.
6. Implement AS-4 as middleware **ahead of the request handler, never inside a tool**.
7. Wire the validated subject into the grant-resolution path (AS-6).
8. Add the connector in Claude and complete the flow on every surface that must be supported — **Claude Code included**, since it is the surface most likely to expose a redirect-URI or client-authentication defect.
9. Implement AS-10 audit logging **before any real content is loaded**.
10. Production: create the production environment, add a payment method, decide on the custom domain.

### 15.2 Adding a person

Five steps, of which the person performs three.

1. An owner enters the new person's email in the admin console. The system creates them at the authorization server and records a profile plus their initial grants.
2. They receive the AuthKit invitation email (sign-in link) — sent when the operator creates them in the WorkOS dashboard (§11.6: no function holds the management key) — and separately the connector URL with the exact click path, which the owner copies from the console and sends however the family communicates.
3. They sign in once in a browser — magic link rather than a password, so nothing has to be chosen, remembered or reset.
4. They add the connector **on web or desktop** and approve the consent screen. **This step cannot begin on a phone.**
5. From then on it works everywhere, phone included.

**The invitation email is the whole onboarding experience** and deserves to be written like a product surface rather than a system notification. Someone receiving it has no administrator to ask and no reason to know what MCP is; if it takes more than one screen, the feature does not work in practice regardless of whether it works technically.

### 15.3 Client constraints that bound the design

These come from the client, not from us, and no amount of server work removes them. **They are the real limits on who can be given access.**

| Constraint | Consequence |
| --- | --- |
| Free plan: one custom connector | A user on Free can hold access to exactly one instance. A second invitation displaces the first. |
| Team/Enterprise: owners add connectors, members connect | Someone inside a managed org cannot self-serve a URL. Their owner must add it organization-wide — a far heavier ask than "click this link." |
| Setup is web/desktop; mobile is use-only | No invitation flow may begin on a phone. Reading on a phone afterward works. |
| Server must be reachable from Anthropic's IP ranges | A self-hosted instance behind a VPN or corporate firewall cannot be connected at all, even though its operator can reach it. |
| Auth settings are fixed once a connector is added | Changing them means removing and re-adding the connector, and every user reconnecting. **The authorization server is not quietly swappable after launch.** |

> **Sharing is bounded by the client, not the server.** The reliable outside-user profile is *a personal Claude account, on web or desktop, connecting to exactly one shared instance.* Everything beyond that — a second instance for a Free user, anyone inside a managed organization — needs a path that does not depend on adding a connector. A product question to settle before designing the invitation, not after.

---

## 16. Quick reference

**Blocking right now:** the four checks in §2. Nothing else starts until they pass.

**The three properties that must not be broken:**

1. **§4.8** — the storage identity carries exactly the user's permissions, enforced by AWS, not by application care. The MCP function has no S3 permission of its own and no grant-write permission; both are IAM facts, not conventions.
2. **§4.7** — no principal or grant operation is reachable from the tool surface, and the role the tool surface runs under could not perform one.
3. **§5.3** — history is per path; `continues_at`, `moved_from` and a forward reference are links a caller may follow, never a traversal the server performs.

**The three things most likely to be got wrong quietly:**

1. Origin validation (§6.2) — a MUST that nothing in the AWS stack provides.
2. The `s3:prefix` condition on a `list` credential (§8.5, step 3) — its failure mode is a full bucket listing, not an error.
3. The unregistered-resource negative test (§2 check 4, §12.8 item 3) — a wrong audience issued silently, with nothing downstream able to detect it.

**The four decisions to re-read before increment B:** archive is a tombstone (§5.2); the pointer is written first (§8.3); one hop per read (§5.3); actor is metadata set from step 8 (§8.2).

**Where the documents used to live:** the five source artifacts (Spec v0.12, Storage v0.3, Auth ADR, Tools v0.1, Build v1) remain in the artifact gallery for provenance. This file supersedes all of them as the working reference.

---

*Engineering handoff · 13 September 2026 · Instance `wiki.famestad.com` · Builder: Josh*
