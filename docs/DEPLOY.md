# Deploying an instance, from zero

The operator sequence for one environment, in the order the steps depend on each
other. Design references are to `HANDOFF.md`. Do `dev` first, end to end, before
touching `prod` — §9.6 puts them in separate AWS accounts, and everything below is
repeated per account.

The four stacks, in dependency order: `wiki-<env>-storage` (key, bucket, grant
table, rate-limit table) → `wiki-<env>-compute` (authorizer, MCP data plane, web
application, storage role) → `wiki-<env>-api` (REST API, custom domain, throttle,
budget) → `wiki-<env>-ops` (trail, alerts, alarms, break-glass role, backups).
`make deploy` deploys all four.

---

## 0. Repository settings (once, before the first real push)

These are GitHub settings, not files, and §12.4 wants them on **before** the first
commit that could carry a credential.

| Setting | Where | Why |
| --- | --- | --- |
| Secret scanning **and** push protection | Settings → Code security | Rejects a credential-bearing commit at push time. [Enable push protection](https://docs.github.com/en/code-security/secret-scanning/enabling-secret-scanning-features/enabling-push-protection-for-your-repository). `gitleaks` in CI is the second net, not the first. |
| Branch protection on `main` | Settings → Branches | Require a pull request; require the `ci / checks` and `ci / secret scan (history)` status checks. |
| Allow auto-merge | Settings → General → Pull Requests | Lets the `dependabot-automerge` job in `.github/workflows/ci.yml` merge patch/minor updates once checks pass. |
| Dependabot | `.github/dependabot.yml` (already in the repo) | Weekly `uv` and Actions updates, grouped minor/patch. |

## 1. AWS accounts and credentials

1. Two accounts: **dev** and **prod**, ideally under one Organization with IAM
   Identity Center (SSO). Never one account with two sets of stacks (§9.6).
2. Sign in with SSO rather than long-lived keys (§8.8 "no long-lived access keys
   anywhere"): `aws configure sso`, then `export AWS_PROFILE=wiki-dev`. Confirm with
   `aws sts get-caller-identity`.
3. Region is `us-west-2` in `infra/config.py`. The ACM certificate must be issued in
   the **same region** as the API (regional endpoint).
4. Bootstrap CDK in each account/region once:
   `npx cdk bootstrap aws://<account-id>/us-west-2`.
5. **Account-level public access block** (§12.3, §12.5). No CloudFormation resource
   exists for it, so it is a command, run once per account:

   ```sh
   aws s3control put-public-access-block --account-id <account-id> \
     --public-access-block-configuration \
     BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   ```

   `make security` asserts it later. Changing it afterwards fires the bucket-guard
   alert (`PutAccountPublicAccessBlock` is on the list).

## 2. Domain and certificate

Pick one of the two routes `infra/stacks/api.py` supports:

- **Hosted zone in this account** — set `hosted_zone_name` in `infra/config.py`. CDK
  issues a DNS-validated certificate and writes the alias record itself. Nothing else
  to do.
- **DNS lives elsewhere** (the current dev config) — request a certificate in ACM,
  `us-west-2`, for the exact hostname (`wiki-dev.famestad.com`), validate it by
  CNAME at your DNS provider, and pass its ARN as `CERT_ARN` to every `make deploy`.
  After the first deploy, create a CNAME (or ALIAS) from the hostname to the
  `DomainTarget` output of the api stack.

## 3. `infra/config.py`

Per environment in `ENVIRONMENTS`:

| Field | dev | prod |
| --- | --- | --- |
| `domain` | `wiki-dev.famestad.com` | `wiki.famestad.com` |
| `hosted_zone_name` | zone name, or `None` for the CERT_ARN route | same |
| `authkit_domain` | the staging AuthKit domain | the **production** AuthKit domain — `REPLACE-ME` must be replaced |
| `object_lock` | `False` | `True` (§8.2 — decided before the first prod deploy; see §8 below) |
| `retain_data` | `False` | `True` |
| `allowed_origins` | `("https://claude.ai",)` | same |
| `log_retention_days` | 90 | 90 (§12.7) |
| `backup_retention_days` | 35 | 35 |

`account` may stay `None` (the CLI's current account is used) or be pinned to the
account id so a deploy with the wrong profile fails instead of succeeding somewhere
surprising. Pin it for prod.

## 4. Deploy

```sh
make sync
make test
make deploy ENV=dev CERT_ARN=arn:aws:acm:us-west-2:<account>:certificate/<id>
```

`make deploy` runs `make build` (packages both functions for arm64 / Python 3.13),
then `cdk deploy --all`. To attach an email to the alerts topic and the budget in the
same deploy, add the context on the CDK command directly:

```sh
make build && npx cdk deploy -c env=dev -c certificateArn=$CERT_ARN -c alertEmail=you@example.com --all --require-approval never
```

Then **confirm the SNS subscription** from the email SNS sends; until you do, every
alarm fires into nothing.

Note the outputs; the later steps use them:

| Stack | Output | Used for |
| --- | --- | --- |
| storage | `BucketName`, `GrantTableName`, `RateLimitTableName`, `KmsKeyArn` | `grant_owner.py`, `make security` |
| api | `CanonicalMcpUrl`, `ResourceMetadataUrl`, `DomainTarget` | WorkOS resource indicator, DNS |
| ops | `AlertsTopicArn`, `TrailArn`, `TrailLogBucketName`, `BackupVaultName`, `BreakGlassRoleArn` | runbook |

Create the DNS record from `DomainTarget` (step 2) and wait for
`curl -i https://wiki-dev.famestad.com/.well-known/oauth-protected-resource/mcp` to
return the metadata document.

## 5. WorkOS (the authorization server)

§15.1 lists the dashboard steps; `scripts/oauth_gate.py` runs the checks that
decide whether WorkOS is acceptable at all (§2). In order:

1. Create the WorkOS account; the staging environment exists immediately. Record the
   AuthKit domain (`https://<slug>.authkit.app`) — it is `authkit_domain` in
   `infra/config.py`. If you set it after the first deploy, deploy again.
2. Connect → Configuration: enable **CIMD**; leave DCR off (AS-8).
3. Connect → Configuration: add `CanonicalMcpUrl` (exactly, byte for byte) as a
   **resource indicator** and make it the default. Dev and prod are registered
   separately, each in its own WorkOS environment (AS-7).
4. Create a public OAuth client for the gate script with redirect URI
   `http://127.0.0.1:8765/callback`.
5. Run the gate:

   ```sh
   uv run python scripts/oauth_gate.py \
     --authkit https://<slug>.authkit.app \
     --client-id client_01... \
     --resource https://wiki-dev.famestad.com/mcp
   ```

   Check 4 (a token for an **unregistered** resource must be refused) decides
   whether the design proceeds. If it issues a token, stop; §2 says what replaces
   WorkOS.
6. Disable self-signup for the environment (§2 check 7) and restrict CIMD origins if
   the dashboard allows it (check 6).
7. The web application (increment F) needs its own OAuth client and a Secrets Manager
   secret; that wave's notes describe the secret's shape.

## 6. First owner

The bootstrap grant is written with **operator** credentials, never a function's —
the MCP role cannot write grants, by design (§8.8).

```sh
uv run python scripts/grant_owner.py --bootstrap \
  --subject user_01H... \
  --table <GrantTableName output>
```

`--subject` is the WorkOS user id (the token's `sub`) — sign in once through the
gate script and read it from the decoded token. `--bootstrap` bypasses the owner
guard for exactly this row (`own` on `/` in an empty table) and logs loudly. Later
grants go through the admin console or the same script with `--granter`.

> `make grant-owner` in the Makefile passes neither `--bootstrap` nor `--granter`,
> so it errors as written; use the script directly until the target is fixed.

## 7. Add the connector

In Claude (web or desktop — this step cannot start on a phone, §15.3): Settings →
Connectors → Add custom connector → URL `https://wiki-dev.famestad.com/mcp`. Complete
the consent screen. Also do it once from Claude Code, which is the surface most
likely to surface a redirect-URI or client-authentication defect (§15.1 step 8).

## 8. `make security` — the §12.8 checklist

`tests/security/` runs the checklist against the deployed instance. The environment
contract, copied from `tests/security/README.md`:

| Variable | Used by | Meaning |
| --- | --- | --- |
| `WIKI_BASE_URL` | every HTTP test | Instance origin, no trailing slash — e.g. `https://wiki-dev.famestad.com`. Must be `https`. |
| `WIKI_TEST_TOKEN` | tokens, zero-grant, item 8 | A live access token for the **bootstrap owner** (`own` on `/`), obtained through the §2 flow. |
| `WIKI_TEST_TOKEN_NOGRANTS` | zero-grant | A live token for a **second user who holds zero grants**. Same AuthKit environment, same scopes. |
| `WIKI_TEST_TOKEN_WRONG_AUD` | tokens (optional) | A genuine AuthKit token issued for a **different** resource indicator. Skipped when unset. |
| `WIKI_TEST_TOKEN_EXPIRED` | tokens (optional) | A once-valid token for this instance whose `exp` has passed. Skipped when unset. |
| `WIKI_TEST_BUCKET` | storage isolation | The instance's bucket name (`BucketName` output). |
| `WIKI_TEST_TABLE` | storage isolation | The grant table name (`GrantTableName` output). |
| `WIKI_TEST_REGION` | storage isolation | Region of both. |

Storage-isolation tests also use the operator's ambient AWS credentials. Tokens are
secrets: export them for the shell that runs the suite and nothing else. **A skip is
never a pass** — read the summary line.

```sh
export WIKI_BASE_URL=https://wiki-dev.famestad.com WIKI_TEST_TOKEN=... WIKI_TEST_TOKEN_NOGRANTS=... \
       WIKI_TEST_BUCKET=... WIKI_TEST_TABLE=... WIKI_TEST_REGION=us-west-2
make security
```

The checklist, item by item, mapped to what proves it:

| §12.8 | How it is verified |
| --- | --- |
| 1. Route table | `make security` → `test_routes.py` |
| 2. Wrong audience / `alg: none` / expired rejected | `test_tokens.py` (expired and wrong-aud need their optional variables) |
| 3. `aud` matches the canonical URI; unregistered URI refused | `test_tokens.py` for the first half; `scripts/oauth_gate.py` check 4 for the refusal (needs a browser) |
| 4. Storage answers only to the storage role | `test_storage_isolation.py` (anonymous access, bucket policy, encryption, IAM sweep); cross-account `GetObject` is a manual check from the other account |
| 5. Zero-grant user sees nothing, mints nothing | `test_zero_grant.py` for the observable half; `tests/unit/tools` asserts `mint_count == 0` |
| 6. No credentials in repo or package | `ci.yml`: gitleaks over the history, the package scan after `make build`; push protection (step 0) |
| 7. Account PAB on, backups under the CMK, **a restore performed** | `test_storage_isolation.py` for the block and the key; `tests/unit/infra/test_ops.py` for the vault key; the restore is a drill — `docs/RUNBOOK.md` "Restore rehearsal", record the date there |
| 8. A 5xx reveals nothing | `test_routes.py` covers the 400 path; the `DEFAULT_5XX` body is fixed in `infra/stacks/api.py`; `wiki-<env>-api5xx` alarms on any real one |

## 9. Production differences

Everything above, again, in the prod account, plus:

- **`object_lock=True`** — the bucket is created with Object Lock on, governance
  mode, one-year default retention (§8.9). `ObjectLockEnabled` is a create-only
  property: it cannot be turned on later without replacing the bucket, so decide
  before the first prod deploy. Nothing that runs code can delete a version for a
  year; hard delete is the break-glass procedure in the runbook.
- **`retain_data=True`** — the bucket, both tables, the key, the trail bucket, the
  backup vault and every log group carry `Retain`. `make destroy ENV=prod` leaves
  them all in place; that is the point.
- **A production WorkOS environment** (payment method required) with its own AuthKit
  domain (`authkit_domain` for prod), its own resource indicator registered for
  `https://wiki.famestad.com/mcp`, and the §2 gate run again against it. A dev
  token must not validate against prod and cannot: the audience differs.
- **Pin `account`** in the prod config.
- **Alerts go to a person.** Pass `-c alertEmail=` on the prod deploy and confirm
  the subscription. Check that the first daily backup job (09:00 UTC) completes in
  the AWS Backup console; a KMS permission problem shows up there and nowhere else.
- Run `make security` against prod with a prod owner token before loading any real
  content (§15.1 step 9, "before any real content is loaded").

## 10. Automating deploys (not yet)

`.github/workflows/scheduled-rebuild.yml` re-resolves dependencies weekly, runs the
checks, and opens a PR. It does **not** deploy: deploying from GitHub Actions needs an
IAM role the workflow assumes through GitHub's OIDC provider, and no such role
exists yet. The job is written in that file, commented out, with
`arn:aws:iam::000000000000:role/wiki-dev-github-deploy` as the placeholder. To
enable it:

1. Create the OIDC identity provider for `token.actions.githubusercontent.com` in
   the dev account.
2. Create a role trusting it with `sub` pinned to `repo:<owner>/<repo>:ref:refs/heads/main`,
   allowed to assume the CDK bootstrap roles (`cdk-hnb659fds-*`) and nothing else.
3. Put the certificate ARN in a repository variable `DEV_CERT_ARN`.
4. Uncomment the job and replace the ARN.

Until then, merging the weekly PR and running `make deploy` is the cadence.
