# Setup guide: WorkOS, AWS, and wiring them together

A start-to-finish walkthrough for standing up one instance: WorkOS as the login
provider, the four AWS stacks, and the settings that connect them, ending with
Claude reading your wiki. Follow it top to bottom for a new environment.

This guide sets the order and says where each value comes from and where it goes.
[`DEPLOY.md`](DEPLOY.md) is the reference behind it: the full first-deploy
checklist, production differences, and the why behind each step. Where this guide
says "see DEPLOY §n", the detail lives there.

## How the pieces fit

```
Claude ──(1) GET /.well-known/oauth-protected-resource/mcp──► your API
       ◄── "my authorization server is https://<slug>.authkit.app"
Claude ──(2) OAuth code + PKCE, resource=https://<domain>/mcp──► WorkOS AuthKit
       ◄── access token, aud = https://<domain>/mcp
Claude ──(3) POST /mcp  Authorization: Bearer <token>──► API Gateway
             authorizer: signature, issuer = AuthKit domain, aud = canonical URL
             MCP function: your grants (DynamoDB) decide every path
```

WorkOS only proves who you are. **What you can see is decided by grants in your own
DynamoDB table**, never by WorkOS. The two systems meet at exactly four values:

| Value | Created in | Goes to |
|---|---|---|
| AuthKit domain `https://<slug>.authkit.app` | WorkOS (the environment you're setting up) | `authkit_domain` in `infra/environments.toml` |
| Canonical MCP URL `https://<domain>/mcp` | You choose `<domain>`; the api stack outputs it as `CanonicalMcpUrl` | WorkOS **resource indicator** (registered and set as default) |
| Web client id + secret | WorkOS (confidential OAuth client for the web app) | id → `workos_web_client_id` in `infra/environments.toml`; secret → the Secrets Manager secret in the `WebSecretArn` output |
| Your WorkOS user id `user_…` | WorkOS (the `sub` in your token) | `make grant-owner SUBJECT=…`, your `own /` grant |

The canonical URL is fixed by the domain you pick, so everything on the WorkOS side
can be done **before** the first deploy.

## Before you start

- **Tools:** `uv`, Node (for `npx cdk`), the AWS CLI v2, a browser.
- **AWS:** an account per environment, reached through SSO (`aws sso login --profile
  <name>`), with CDK bootstrapped in `us-west-2`. See DEPLOY §1, which also covers the
  one-time account-level S3 public access block.
- **A domain you control**, e.g. `wiki-dev.example.com`. Its DNS zone may be in a
  different AWS account, or outside AWS altogether. You will add two records: a
  certificate-validation CNAME and a CNAME/ALIAS to the API.
- **Pick the hostname now.** It becomes the token audience, and changing it later
  means re-registering the resource in WorkOS and every user reconnecting.

---

## Part 1 — WorkOS

Use the WorkOS **staging** environment for dev. Production needs its own environment
(payment method required) and a repeat of this part (DEPLOY §9).

1. **Record the AuthKit domain.** The environment's AuthKit domain looks like
   `https://<slug>.authkit.app`.
2. **Connect → Configuration:** enable **Client ID Metadata Document (CIMD)**. Leave
   **Dynamic Client Registration off**. Claude registers itself by CIMD.
3. **Connect → Configuration:** add `https://<domain>/mcp` as a **resource
   indicator**, exactly as the server will announce it (no trailing slash), and make
   it the **default**.
4. **Authentication → Features → Sign-up:** disable self-signup. People are invited,
   never self-registered.
5. **Authentication → Methods:** enable **Magic Auth** and turn **Email + Password
   off**. The web app's login page promises an emailed link; the WorkOS defaults show
   a password prompt instead.
6. **Create the gate-test client** under **Connect → Applications → Create
   application → OAuth application**, with **Use PKCE** ticked and redirect URI
   `http://127.0.0.1:8765/callback`.
   > Don't create it from the top-level **Applications** menu. That client id is
   > rejected by `/oauth2/*` with `application_not_found`.
7. **Create the web app's client** the same way (Connect → Applications → OAuth
   application). Make it **confidential**, with authorization code + PKCE and redirect
   URI exactly `https://<domain>/app/callback`. Keep its **client id** and **client
   secret** for Part 3.
8. **Don't create custom scopes or a management API key.**
   - Scopes: ADR-0016. A CIMD-registered client can't be assigned custom scopes, so
     requesting them fails with `invalid_scope`. Tokens carry `openid profile email`,
     and grants do the authorizing.
   - Management key: no function needs one, and it could change any user's email.
9. **Create your own user** (Users → Create user, then Invite) if you don't have one
   yet.
10. **Run the gate.** It talks only to AuthKit, so it works before anything is
    deployed:

    ```sh
    uv run python scripts/oauth_gate.py \
      --authkit https://<slug>.authkit.app \
      --client-id client_01...          # the gate-test client from step 6
      --resource https://<domain>/mcp
    ```

    It opens the browser twice. It must end with `GATE PASSED`. Check 4, which
    refuses a token for an *unregistered* resource, is the one that matters. **Copy
    the `sub` from the decoded token (`user_…`); Part 3 needs it.** Check 6 (a CIMD
    origin allowlist) can't pass because WorkOS doesn't offer one; the script prints
    it as a reminder only.

## Part 2 — AWS

1. **Fill in `infra/environments.toml`** for the environment — copy
   `infra/environments.example.toml`; the file is gitignored, so your account ids and
   domains stay out of the repo. Any `EnvConfig` field in `infra/config.py` can be set:

   | Field | Value |
   |---|---|
   | `account` | the 12-digit account id (prod must pin it; synth refuses credentials that resolve elsewhere) |
   | `domain` | `<domain>` |
   | `hosted_zone_name` | the Route 53 zone name if it's in *this* account, otherwise `None` |
   | `authkit_domain` | from Part 1 step 1 |
   | `workos_web_client_id` | from Part 1 step 7 |

   Leave the rest at their defaults (DEPLOY §3 explains each one).

2. **Log in and confirm the account:**

   ```sh
   aws sso login --profile <profile>
   AWS_PROFILE=<profile> aws sts get-caller-identity   # must show the account you pinned
   ```

3. **Certificate.** Skip this if `hosted_zone_name` is set, because CDK issues it
   then. Otherwise issue it by hand, once:

   ```sh
   AWS_PROFILE=<profile> make cert-deploy ENV=dev     # waits for DNS validation
   AWS_PROFILE=<profile> make cert-status ENV=dev     # in another shell: prints the CNAME
   ```

   Add the printed CNAME at your DNS provider and **leave it there forever**, because
   ACM renews through it. The deploy finishes once ACM validates. The ARN is stored
   in SSM, so `make deploy` finds it without `CERT_ARN=`.

4. **Deploy:**

   ```sh
   make sync && make test
   AWS_PROFILE=<profile> make synth ENV=dev     # optional dry run; prints env/account/region
   AWS_PROFILE=<profile> make deploy ENV=dev
   ```

   All four stacks deploy (storage → compute → api → ops). Note these outputs:
   `CanonicalMcpUrl`, `DomainTarget` and `ResourceMetadataUrl` (api stack),
   `WebSecretArn` (compute stack), and `GrantTableName` and `BucketName` (storage
   stack).

   To get alarm emails, deploy with `-c alertEmail=you@example.com` (DEPLOY §4) and
   **confirm the SNS subscription email**. Until then, alarms go nowhere.

5. **Check `CanonicalMcpUrl`.** It must equal the resource indicator you registered in
   Part 1 step 3, byte for byte.

## Part 3 — Connect them

1. **DNS.** Create a CNAME (or ALIAS) from `<domain>` to the `DomainTarget` output.
   Then check that the metadata answers and names your AuthKit domain:

   ```sh
   curl -s https://<domain>/.well-known/oauth-protected-resource/mcp
   # → "resource": "https://<domain>/mcp", "authorization_servers": ["https://<slug>.authkit.app"]
   ```

2. **Web app secret.** In Secrets Manager, open the secret named by `WebSecretArn` and
   replace the `REPLACE-ME` value of `client_secret` with the web client's secret from
   Part 1 step 7. Leave `session_key` alone; it was generated for you. Until this is
   done, web login fails at the token exchange.

3. **Make yourself the owner.** This uses operator credentials, because the functions
   can't write grants by design:

   ```sh
   AWS_PROFILE=<profile> make grant-owner \
     SUBJECT=user_01...  EMAIL=you@example.com  NAME="Your Name" \
     TABLE=<GrantTableName output>
   ```

   This gives you `own` on `/` and writes the profile row the web app needs at
   sign-in. It refuses to run again once `/` has an owner.

4. **Add the connector in Claude.** On web or desktop (not a phone), go to Settings →
   Connectors → Add custom connector, enter `https://<domain>/mcp`, and complete the
   WorkOS sign-in. In Claude Code, add it as a remote MCP server with the same URL.

5. **Verify.**
   - Ask Claude to call `list_folder` on `/`. You should see the root (empty on a new
     instance).
   - `shared_with_me` should list `own` on `/`.
   - Sign in to the web app at `https://<domain>/app`.
   - Then run the security suite (DEPLOY §8) and the negative storage tests (`make
     aws-tests`). **A skipped test is not a pass.**

## Adding people

The dashboard first, then the console. The app never calls WorkOS.

1. WorkOS → Users → **Create user**, then **Invite**. Copy their `user_…` id.
2. Web app → Admin console → People → **Add a person**. Paste the id and set where
   they get access. The console shows the connector steps to send them.

They see only what their grants cover. To see what has been shared with them, they
ask their agent to run `shared_with_me`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `application_not_found` from `/oauth2/*` | The client was created from the top-level Applications menu | Recreate it under **Connect → Applications → OAuth application** (Part 1 step 6) |
| `invalid_scope` | Custom scopes requested | Request only `openid profile email` (plus `offline_access` if needed). ADR-0016 |
| A password prompt at first login | WorkOS defaults | Magic Auth on, Email + Password off (Part 1 step 5) |
| Web login fails after the WorkOS redirect | `client_secret` still `REPLACE-ME`, or redirect URI mismatch | Part 3 step 2; the redirect must be exactly `https://<domain>/app/callback` |
| Connector fails with an audience or invalid-token error | Resource indicator ≠ `CanonicalMcpUrl` | Make them byte-identical (a trailing slash counts), then remove and re-add the connector |
| Synth stops: `credentials resolve to account X but the config pins Y` | Wrong profile, or an expired SSO session | `aws sso login --profile <right one>`, then check `aws sts get-caller-identity` |
| `Token has expired and refresh failed` | The SSO session expired | `aws sso login --profile <profile>` |
| Bare `aws` commands can't find the stacks | The profile's default region isn't `us-west-2` | Add `--region us-west-2`. CDK is unaffected |
| `cert-deploy` hangs at validation | The CNAME went to the wrong zone | The CNAME goes in the zone that serves `<domain>`, which may be a different AWS account |
| The metadata URL doesn't answer | DNS not created or not propagated yet | Part 3 step 1; check with `dig <domain>` |
| Changed auth settings, and now users can't connect | Claude caches auth settings per connector | Remove and re-add the connector; every user does the same (HANDOFF §15.3) |
| The agent can connect but sees nothing | No grants for that user | `make grant-owner` for the first owner; the admin console for everyone else |

## Next

- **Production:** repeat all three parts with a production WorkOS environment,
  `object_lock=True`, `retain_data=True`, a pinned account and an alert email.
  DEPLOY §9.
- **Day two:** restore, break-glass, revoking access, alarms — [`RUNBOOK.md`](RUNBOOK.md).
