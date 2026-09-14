# tests/security — the §12.8 pre-launch checklist, automated

Integration tests against a **deployed development instance** (HANDOFF §12.8, build
step 9). They are marked `security`, the default `pytest` run deselects them
(`addopts` in `pyproject.toml`), and `make security` runs them:

```sh
make security
# equivalently
uv run pytest -m security -o addopts=""
```

Every test reads the variables below and **skips, with the variable named, when one it
needs is absent**. A skip is never a pass: read the summary line, and treat a run
that is mostly skips as a run that proved nothing.

## Environment contract

| Variable | Used by | Meaning |
| --- | --- | --- |
| `WIKI_BASE_URL` | every HTTP test | Instance origin, no trailing slash — e.g. `https://wiki-dev.famestad.com`. Must be `https`. |
| `WIKI_TEST_TOKEN` | tokens, zero-grant, item 8 | A live access token for the **bootstrap owner** (`own` on `/`), obtained through the §2 flow (a real authorization-code + PKCE round trip against the dev resource indicator). |
| `WIKI_TEST_TOKEN_NOGRANTS` | zero-grant | A live token for a **second user who holds zero grants**. Same AuthKit environment, same scopes (`wiki.read wiki.write`) — the tests exercise the grant layer, not the scope layer. |
| `WIKI_TEST_TOKEN_WRONG_AUD` | tokens (optional) | A genuine AuthKit token issued for a **different** resource indicator. Skipped when unset. |
| `WIKI_TEST_TOKEN_EXPIRED` | tokens (optional) | A once-valid token for this instance whose `exp` has passed (tokens live ≤ 15 min, AS-9 — keep one from an earlier session). Skipped when unset. |
| `WIKI_TEST_BUCKET` | storage isolation | The instance's bucket name. |
| `WIKI_TEST_TABLE` | storage isolation | The grant table name. |
| `WIKI_TEST_REGION` | storage isolation | Region of both. |

Storage-isolation tests also need the operator's **ambient AWS credentials** (profile,
SSO, or exported keys) for the bucket-policy, encryption, and IAM reads; the anonymous
attempts use a `botocore.UNSIGNED` client and never see them. Tests that need a
permission the ambient role lacks skip and name the permission.

The names are deliberately distinct from the runtime set (`WIKI_BUCKET`,
`GRANT_TABLE`, …) because `tests/conftest.py` overwrites those — and
`AWS_ACCESS_KEY_ID` and friends — with fakes for every test. `tests/security/conftest.py`
snapshots the `AWS_*` environment at import time and builds its `boto3.Session` from
the snapshot, so the fakes never reach a real call.

Tokens are secrets. Export them for the shell that runs the suite and nothing else;
never write them to a file in the repository (§12.3, §12.7).

## What each file proves

| File | §12.8 item | Assertions |
| --- | --- | --- |
| `test_routes.py` | 1 | Both metadata documents: 200, `resource` == `WIKI_BASE_URL + "/mcp"` byte for byte, exactly one `authorization_servers` entry (AS-2), both scopes advertised. `OPTIONS /mcp` unauthenticated with CORS headers exposing `WWW-Authenticate` and allowing the MCP header set. `GET`/`DELETE /mcp` → 405. `POST /mcp` without a token → 401 with `Bearer resource_metadata="<metadata URL>"`, a `scope`, and `Access-Control-Expose-Headers` on the gateway response itself (§9.3). `/`, `/health`, `/metrics`, `/admin`, `/mcp/x`, `/.well-known/nope`, a random path → 404 with no "Missing Authentication Token", no traceback, no request echo. |
| `test_routes.py` | 8 | A non-JSON body with a valid token → 400 whose body carries no `Traceback`, no `/var/task`, no `Exception`, and does not echo the request. |
| `test_tokens.py` | 2 | 401 for: garbage bearer; a self-signed RS256 JWT with the real `iss`/`aud`; an `alg: none` JWT; a genuine wrong-audience token (optional var); a genuine expired token (optional var). Each rejection must carry the discovery challenge (AS-3). |
| `test_tokens.py` | 3 | `WIKI_TEST_TOKEN` decoded **without** verification: `aud` equals the metadata document's `resource` byte for byte (§2 check 3 as an assertion), `iss` is the advertised authorization server, and the `scope` claim carries both scopes (§2 check 5). Plus the positive path: `ping` → JSON-RPC result, `tools/list` contains `read_article`. |
| `test_zero_grant.py` | 5 (observable half) | With the zero-grant token: `list_folder /` → tool error 404; `read_article /anything.md` → 404, **not** 403 (§10.4 indistinguishability); `search` (when deployed) → empty hits; `create_article /probe.md` → tool error `403 forbidden` whose message names no scope (§6.5 row 3), all inside HTTP 200 with no `WWW-Authenticate`. Then, as the owner, `/probe.md` does not exist. |
| `test_storage_isolation.py` | 4, 7 (part) | Anonymous `GetObject a/probe.md` and `ListObjectsV2` → `AccessDenied`. Bucket policy status not public; policy contains a Deny over `Principal: *` conditioned on the caller's account/ARN/org; bucket and account public-access blocks all four on. Bucket SSE-KMS and table SSE `KMS`, both under a **customer-managed** key. IAM sweep: no user holds — inline, attached, or via a group — a policy whose document names the bucket or the table. |

Every response is also checked, informationally, for stack-naming headers (`server`,
`x-powered-by`, …). Those surface as pytest warnings, never failures.

### The probe article

`test_zero_grant.py` tries to create `/probe.md` as the zero-grant user and then
confirms, as the owner, that it does not exist. If the create ever succeeds the file
stays in the bucket and every later run fails at the precondition with "stale
/probe.md exists". That is the intended behaviour: it is a security finding, not a
flaky test. Remove the object and find out why the create went through.

## What is not automated here, and why

Item by item, so nobody reads a green run as more than it is.

- **Item 2, expired token.** Only genuine when the token was genuinely valid once, so
  it needs `WIKI_TEST_TOKEN_EXPIRED` from the operator; without it the test skips. The
  expiry check in isolation is a unit test in `tests/unit/authorizer`.
- **Item 3, "request one for an unregistered URI and confirm refusal".** Driving an
  authorization-code + PKCE flow needs a browser login; it stays a manual §2 check 4.
  What *is* automated is the consequence: the owner's `aud` equals the published
  `resource` byte for byte.
- **Item 4, "GetObject from a role in another account".** Needs a second AWS account.
  Not automated. The bucket-policy Deny statement and the public-access blocks are
  the controls that make it true and are asserted directly.
- **Item 4, "GetItem on the grant table from any principal other than the two
  application roles".** Cannot be asserted from the operator's own role, which is
  allowed by design; DynamoDB rejects unsigned requests before authorization, so an
  anonymous attempt proves nothing. Asserted instead: SSE-KMS under the customer key,
  and the IAM sweep. A table resource policy, if one is added, would be the thing to
  read here.
- **Item 4, the IAM sweep, honestly.** It matches policy documents against the bucket
  and table *names*. A user with `Resource: "*"` (AdministratorAccess) names neither
  and is not caught. Users with active access keys are reported as a warning because
  §8.8 says there should be none — it is a warning rather than a failure so an operator
  who has not yet migrated to SSO can still run the rest of the suite. The sweep skips
  when the ambient role lacks `iam:ListUsers`.
- **Item 5, "no `AssumeRole` call was made".** CloudTrail delivers minutes later and
  cannot be asserted on inside a test. This half is a unit test in `tests/unit/tools`
  against `CredentialMinter.mint_count == 0` for a subject with no grants; the tests
  here cover what a zero-grant user can observe.
- **Item 6, credential scan of the repository and package.** Not an HTTP or AWS
  assertion; belongs to CI (push protection plus a secret scanner over the build
  directory), not to this suite.
- **Item 7, backups encrypted with the customer key and a restore actually
  performed.** No backups exist in the skeleton, and a restore is a drill someone
  performs, not a test. The account-level public access block and the customer-managed
  key on bucket and table *are* asserted.
- **Item 8, a genuine 5xx.** There is no way to trigger a server error on demand from
  outside without a fault-injection hook, and a hook would be a security surface of its
  own. The 400 path is asserted; the `DEFAULT_5XX` gateway response body is fixed in
  `infra/stacks/api.py` and is the same generic shape.
