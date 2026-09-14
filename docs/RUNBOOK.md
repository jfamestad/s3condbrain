# Runbook

What to do when something needs doing by hand. Section references are to
`HANDOFF.md`. Every procedure here assumes `AWS_PROFILE` points at the right account
(§9.6 — dev and prod are separate accounts; check `aws sts get-caller-identity`
before anything below) and that the stack outputs are at hand (`docs/DEPLOY.md` §4).

Naming used throughout: `<bucket>` is the storage stack's `BucketName` output,
`<key-arn>` its `KmsKeyArn`, `<table>` its `GrantTableName`, `<env>` is `dev` or
`prod`. Article `/racing/setup.md` is object key `a/racing/setup.md` (§8.2).

---

## 1. Restore a single article version

Nothing is ever overwritten in place: every write appends a version to the key
(§8.2, §8.9). Restoring is therefore **re-putting an older body as the newest
version**. The old versions stay underneath.

### 1a. Through the MCP tools (preferred)

Works for anyone with `write` on the path, keeps attribution honest, and needs no
AWS access. From any connected client (or `scripts/` with an owner token):

1. `list_versions(path)` — pick the `version_id` you want back. Entries carry
   `kind` and `actor`, so "the last version before the agent rewrote it" is
   readable.
2. `read_version(path, version_id)` — returns `content` and `frontmatter`.
3. `read_article(path)` — returns the **current** `version` token.
4. `update_article(path, content=<step 2 content>, if_version=<step 3 version>,
   frontmatter=<step 2 frontmatter if wanted>)`.

The result is a new version with `kind: write` and your subject as `actor`. If the
article is archived, `unarchive_article` first; if it was moved, the path in step 1
is the new one (`read_article` on the old path returns a forward reference).

### 1b. Through S3 directly

Only when the tool path is unavailable (the API is down, or the article sits
outside anyone's grants). Use an administrator session; the break-glass role has
no `PutObject` on purpose — restores are not what it is for.

```sh
KEY=a/racing/setup.md
aws s3api list-object-versions --bucket <bucket> --prefix "$KEY" \
  --query 'Versions[?Key==`'"$KEY"'`].{id:VersionId,at:LastModified,latest:IsLatest,size:Size}' \
  --output table

# inspect the candidate's attribution before trusting it
aws s3api head-object --bucket <bucket> --key "$KEY" --version-id <VersionId> \
  --query '{actor:Metadata.actor,kind:Metadata.kind,ct:ContentType}'

aws s3api get-object --bucket <bucket> --key "$KEY" --version-id <VersionId> /tmp/restore.md

# re-put as the newest version. Metadata is REQUIRED: without it the version
# reads as `actor: unknown` in list_versions (§8.2).
aws s3api put-object --bucket <bucket> --key "$KEY" --body /tmp/restore.md \
  --content-type 'text/markdown; charset=utf-8' \
  --metadata actor=human:<your-subject>,kind=write
rm /tmp/restore.md
```

Bucket default encryption applies (SSE-KMS under `<key-arn>`), so no encryption
flags are needed. The listing projection for the folder refreshes itself on the
next read (increment A); if it looks stale, any write in the folder forces it.

## 2. Restore a folder

The same operation for every key under a prefix, choosing "the newest version
before time T" for each. Object Lock (prod) is irrelevant here — nothing is deleted.

```sh
PREFIX=a/racing/
T=2026-09-01T00:00:00Z
aws s3api list-object-versions --bucket <bucket> --prefix "$PREFIX" --output json \
  > /tmp/versions.json

# For each key, the newest version whose LastModified <= T. Skip _listing.json;
# it is rebuilt. Review this list BEFORE running the restore loop.
jq -r --arg t "$T" '
  .Versions
  | map(select(.LastModified <= $t and (.Key | endswith("/_listing.json") | not)))
  | group_by(.Key)
  | map(max_by(.LastModified))
  | .[] | "\(.Key)\t\(.VersionId)"' /tmp/versions.json | tee /tmp/restore-plan.tsv

while IFS=$'\t' read -r key vid; do
  aws s3api get-object --bucket <bucket> --key "$key" --version-id "$vid" /tmp/body >/dev/null
  aws s3api put-object --bucket <bucket> --key "$key" --body /tmp/body \
    --content-type 'text/markdown; charset=utf-8' \
    --metadata actor=human:<your-subject>,kind=write >/dev/null
  echo "restored $key from $vid"
done < /tmp/restore-plan.tsv
rm -f /tmp/body /tmp/versions.json /tmp/restore-plan.tsv
```

Caveats worth reading twice:

- A key whose newest-before-T version is a **pointer** (`type: pointer`) or an
  **archive tombstone** (`type: archived`) is restored to that state, which is
  usually right — it was moved or archived before T. If you want the content
  back, pick an earlier content version for that key (1b) or use
  `unarchive_article`.
- Keys created **after** T are left as they are; a folder restore does not delete.
  Archive them through the tools if they should not exist.
- Do this in dev first with the same script and a synthetic folder. It is a loop of
  `put-object`; the only thing that can go wrong is the plan, so read the plan.

## 3. Hard delete — the break-glass procedure

**There is deliberately no function, tool, or admin-console button that can do
this.** §8.9 puts hard delete where a person has to go looking for it, and §8.8
gives the permission to one role that nothing running code can assume:
`wiki-<env>-break-glass` (`BreakGlassRoleArn` output). Every assumption of it fires
the `wiki-<env>-break-glass-used` alert; every delete it performs is a write data
event in the trail.

When it is warranted: content that must not exist in any version (something pasted
by mistake that is not the family's to keep), or a legal request. Not for "it is
untidy" — that is `archive_article`.

1. **Open a ticket** (an issue, a note in the ops log — anything with a date and
   your name). Record: the path(s), the version ids or "all", why, who asked.
   The alert email that arrives when you assume the role is the second half of the
   record; keep both.
2. **Switch role in the console**: account id, role name `wiki-<env>-break-glass`.
   Sessions last one hour. Or on the CLI:

   ```sh
   CREDS=$(aws sts assume-role --role-arn <BreakGlassRoleArn> \
     --role-session-name "hard-delete-$(date +%Y%m%d)-<ticket>" --query Credentials --output json)
   export AWS_ACCESS_KEY_ID=$(jq -r .AccessKeyId <<<"$CREDS") \
          AWS_SECRET_ACCESS_KEY=$(jq -r .SecretAccessKey <<<"$CREDS") \
          AWS_SESSION_TOKEN=$(jq -r .SessionToken <<<"$CREDS")
   ```

   The session name lands in CloudTrail; put the ticket reference in it.
3. **List every version** of the key, including delete markers. Hard delete means
   every one of them; leaving one means it is not gone.

   ```sh
   KEY=a/racing/setup.md
   aws s3api list-object-versions --bucket <bucket> --prefix "$KEY" \
     --query '{v:Versions[?Key==`'"$KEY"'`].VersionId, d:DeleteMarkers[?Key==`'"$KEY"'`].VersionId}'
   ```

4. **Delete each version**. In prod the versions are under Object Lock governance
   retention for a year (§8.9); the role holds `s3:BypassGovernanceRetention`, and
   the flag must be passed explicitly or S3 refuses:

   ```sh
   for vid in <VersionId ...>; do
     aws s3api delete-object --bucket <bucket> --key "$KEY" --version-id "$vid" \
       --bypass-governance-retention
   done
   ```

   In dev (no Object Lock) omit the flag. `delete-object` without `--version-id`
   only adds a delete marker; that is not a hard delete.
5. **Confirm** the list in step 3 is empty, then `unset AWS_ACCESS_KEY_ID
   AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN` (or sign out of the switched role).
6. The folder's `_listing.json` still names the article until the next rebuild;
   force one with any write in that folder, or delete the listing object the same
   way (it is regenerated). Search results follow the listing.
7. **Close the ticket** with the alert email attached and the count of versions
   removed. Note in it whether the content had reached anyone's context before the
   delete — §12.9: revocation and deletion are prospective; a hard delete does not
   pull text back out of a conversation.

Why the role has no `PutObject`: so that a break-glass session cannot quietly
rewrite history as well as erase it. Restores are section 1.

## 4. Rotations

### 4a. The KMS key

Automatic. `enable_key_rotation=True` on `wiki-<env>` rotates the backing material
yearly; the key ARN, alias, and every existing ciphertext stay valid, and nothing
in the application or the stacks needs to change. There is no manual step and no
alarm for it. Do **not** replace the key with a new one: every object, both tables,
the trail bucket and the backup vault are bound to this ARN.

### 4b. The WorkOS client secret (web application)

The MCP path has no client secret — the authorizer validates tokens against the
JWKS endpoint (AS-4). The web application (increment F) is a confidential OAuth
client whose secret lives in one Secrets Manager secret (`WEB_SECRET_ARN`). To
rotate:

1. WorkOS dashboard → the web application's client → generate a new secret. Both
   the old and the new are valid until you delete the old.
2. `aws secretsmanager put-secret-value --secret-id <WEB_SECRET_ARN>
   --secret-string '{"client_secret":"<new>","session_key":"<current>"}'`.
   `put-secret-value` replaces the whole string, so read the current value first
   (`get-secret-value`) and carry `session_key` over unchanged — a changed
   `session_key` logs everyone out (4d).
3. Wait one function lifetime for warm containers to recycle (or redeploy the
   compute stack), then confirm a fresh browser login works.
4. Delete the old secret in the WorkOS dashboard. A login that fails at this point
   means a container still holds the old value; repeat step 3.

### 4c. Anything else

There are no long-lived AWS access keys (§8.8), and no function holds a WorkOS
management API key (HANDOFF §11.6 — people are created in the WorkOS dashboard and
recorded in the console), so there is nothing else to rotate. If an access key ever
exists, `make security` reports it as a warning; treat that as a finding. If a
management API key ever exists, that is a design regression, not a rotation item.

### 4d. Rotation cadence

Nothing alarms on an overdue rotation; put the dates in a calendar that is read.

| What | When | How |
| --- | --- | --- |
| WorkOS client secret (`client_secret` in the web secret) | Every **90 days**, and on any suspicion of exposure | 4b: create the new secret in the WorkOS dashboard → `put-secret-value` with the new value, other fields carried over → verify a fresh browser login → revoke the old secret in the dashboard. |
| WorkOS management API key | — | None exists: no function holds one (HANDOFF §11.6). People are created in the WorkOS dashboard and recorded through the console. |
| Session key (`session_key` in the same secret) | Only when needed — a suspected theft of *every* cookie, an operator leaving | Edit the secret with a new random value of the same shape (48 alphanumerics). **This logs everyone out**: every session cookie was signed with the old key and stops validating as soon as a container reads the new one. Tell people before, not after. One person's sessions end without a key change: their own "Sign out everywhere" button bumps their profile's `session_epoch`, and every cookie issued before it is refused on its next request (HANDOFF §11.6). |
| KMS key `wiki-<env>` | Automatic, yearly | Nothing to do (4a). Never replace the key. |
| Long-lived AWS access keys | — | None exist (4c). |

## 5. Revoke a person

Three places, in this order; each is independent and each is necessary.

1. **Revoke their grants.** Every row for the subject, through the console or
   `GrantAdmin.revoke` as an owner of each node. This is what stops access: the
   data plane resolves grants on every call (AS-6, cache TTL zero), so a token they
   still hold answers `404`/`403` from the next call on. Zero grants also means
   nothing is minted for them (§4.8).
2. **Disable the profile.** In the admin console (or through `GrantAdmin.set_status`
   with operator credentials): status → `disabled`. As of the skeleton the data
   plane resolves grants without reading the profile, so this step is the record
   and the guard against a re-grant, not the enforcement — step 1 is. When the
   status check lands in the data plane (increment D), this step becomes
   sufficient on its own; keep doing both.
3. **Deactivate them at WorkOS** (dashboard → Users → deactivate, or delete). This
   stops new tokens; step 1 stops existing ones. Do both.

Then say this plainly to whoever asked, because §12.9 requires it: **revocation is
prospective.** It stops future reads. It does not remove anything already placed in
the person's conversation history, notes, or an agent's context. If the concern is
what they already saw, the answer is "they saw it", not a setting.

## 6. Alarms — what each one means and what to do

All of them publish to `wiki-<env>-alerts` (`AlertsTopicArn`). If nobody receives
that topic, none of this matters: confirm the subscription after every fresh deploy.

| Alarm / rule | Fires when | First response |
| --- | --- | --- |
| `wiki-<env>-api5xx` | API Gateway returned any 5xx in 5 min | Check `wiki-<env>-mcperrors` first; if it is quiet, the 5xx is the gateway's own (integration timeout at 29 s, authorizer crash rather than rejection). Look at the API's execution log for the request id. |
| `wiki-<env>-authfailures` | ≥ 20 authorizer errors in 5 min | Every rejection is a raise, so this is normally a client retrying a bad token — one person's stale connector. Find the subject in the authorizer log. Sustained from many sources with no known user: check that the JWKS endpoint is reachable (an outage there rejects everyone) and that `authkit_domain` was not changed. |
| `wiki-<env>-mcperrors` | any unhandled crash in the data plane | The tool boundary logs `tool_crash` with a stack (§12.3: the response body says nothing, the log says everything). Search the MCP log group for `tool_crash`. |
| `wiki-<env>-mcpthrottles` | Lambda throttled the MCP function | Concurrency, not the gateway throttle (that returns 429 without invoking). A loop somewhere: find the top subject in the application log (`subject`, `tool` per line) and rate-limit or disable them. Raise the account's concurrency only if the traffic is legitimate. |
| `wiki-<env>-listingfailures` | ≥ 5 `listing_refresh_failed` / `listing_rebuilt_unpersisted` in 15 min | Readers still get correct answers from a live rebuild; the projection just is not being saved. Usually two writers racing on one folder — benign, self-clearing. Sustained: check the storage role's `s3:PutObjectTagging` on listing keys and the function's log for the S3 error. |
| `wiki-<env>-bucket-guard` (rule) | A bucket policy, ACL, public-access block, versioning, Object Lock, encryption or lifecycle change on **any** bucket in the account, or the account-level block | If it was you deploying, it matches the deploy. If not, treat it as an incident: the event carries the principal; check CloudTrail for what else that principal did, and restore the setting. |
| `wiki-<env>-break-glass-used` (rule) | Someone assumed the break-glass role | There should be a ticket (section 3). If there is not, someone with account admin used it without the procedure — find out who from the `AssumeRole` event and what they deleted from the trail's S3 write events. |
| Budget `wiki-<env>-monthly` | 80 % of 20 USD in the month | Almost always CloudTrail data events or a loop. Cost Explorer by service, then the application log by subject. |

Not alarmed, by design: 4xx counts (noisy, and every denial is already a log line —
§12.7), and DynamoDB backups shared outside the account (§12.5 asks for it, but
AWS Backup recovery points for DynamoDB **cannot be shared** to another account —
there is no API call to alarm on; the vault's key is the control, and the vault
access policy is the place to look if that ever changes).

## 7. Backups and the restore rehearsal (§12.8 item 7)

What exists:

- **Articles**: S3 versioning is the backup, Object Lock in prod is what stops it
  being deleted (§8.9). There is no second copy of the bucket; a second copy would
  be the disclosure path §12.5 warns about, and versioning already survives every
  failure short of losing the bucket.
- **Grants**: DynamoDB point-in-time recovery (35 days, continuous) plus an AWS
  Backup daily snapshot at 09:00 UTC into vault `wiki-<env>-grants`, kept 35 days,
  encrypted with the same customer-managed key. Two mechanisms because they fail
  differently: PITR dies with the table; the vault does not.
- **Rate-limit counters**: not backed up. They expire on their own.
- **CloudTrail**: the trail bucket, 90 days.

**Rehearse the restore once, then once a year, and write the date below.** A backup
that has never been restored is a hope.

### Rehearsal (dev account, ~20 minutes)

1. Article: pick a synthetic article, `update_article` it twice, then restore the
   first version by 1a. Confirm `list_versions` shows three content versions and
   the newest body equals the first.
2. Folder: run section 2 against a synthetic folder with `T` set between two
   writes. Confirm the plan matched expectation before the loop ran.
3. Grant table, from the vault: AWS Backup console → vault `wiki-dev-grants` → newest
   recovery point → Restore → **new table name** (`wiki-dev-grants-rehearsal`).
   Wait for it. `aws dynamodb scan --table-name wiki-dev-grants-rehearsal --select COUNT`
   should match the live table's count. Then delete the rehearsal table.
   (A real restore would then repoint `GRANT_TABLE` on the compute stack at the
   restored table or copy the rows back; the point of the rehearsal is knowing the
   recovery point is readable and the key permits it.)
4. Grant table, from PITR: `aws dynamodb restore-table-to-point-in-time
   --source-table-name <table> --target-table-name wiki-dev-grants-pitr
   --use-latest-restorable-time`, confirm the count, delete it.
5. Hard delete: run section 3 on a synthetic article in **dev** so that the
   procedure has been executed by a person before it is needed in anger, and so
   the `break-glass-used` alert is known to arrive.

### Record

| Date | Environment | Who | Steps done (1–5) | Notes |
| --- | --- | --- | --- | --- |
| _not yet performed_ | | | | The §12.8 checklist is not complete until a row exists here. |

## 8. Things that are out of scope, stated so nobody looks for them

- **AWS Config** — not deployed. The bucket-guard rule and the trail cover the
  changes that matter to this design; Config rules would add a monthly charge and
  a second console to read.
- **Snapshot-sharing alarm** — N/A for DynamoDB, see section 6.
- **CloudTrail S3 read events** — off, on purpose (§12.7). The application log
  records every read.
- **`make destroy ENV=dev`** with a non-empty backup vault fails: AWS refuses to
  delete a vault holding recovery points. Delete them in the console first, or
  wait for the 35-day lifecycle. Prod vaults are retained regardless.
