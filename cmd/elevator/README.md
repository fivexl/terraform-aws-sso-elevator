# elevator

Submit a temporary AWS access request without Slack.

## Why this exists

The normal SSO Elevator flow happens entirely in Slack: you post a request, an approver clicks Approve, and the module grants a temporary permission set. That works well for a person, but it's awkward for a script, a CI job, or an AI coding agent that can't click a button — they need a command that submits the same request and reports a clear result via its exit code (0 on success, non-zero otherwise) and human-readable output on stdout/stderr. There's no `--json` / structured-output mode yet, so a caller that needs to parse the result programmatically (rather than just check the exit code) has to parse this prose output itself. `elevator` signs the request with your own local AWS credentials and posts it directly to the module's `POST /access-requester-cli` route, with a proof of your identity (see [How your identity is proved](#how-your-identity-is-proved)). The request then goes through the same approval pipeline as a Slack request: same approvers, same self-approval rules, same audit log.

You must call from an account in the module's AWS Organization. Outside the module's own account, your permission set also needs `execute-api:Invoke` on the module's `requester_api_execution_arn_cli` output ([example statement](https://github.com/fivexl/terraform-aws-sso-elevator/blob/main/docs/cli.md#requirements)).

You must sign with an IAM Identity Center (SSO) session, for example after `aws sso login`. IAM users, other roles, and CI/OIDC roles are rejected, so a CI job needs a real SSO session too. Your Identity Store user's primary email must belong to a Slack user in the workspace. The operator side, including the full requirements and trust model, is in [docs/cli.md](https://github.com/fivexl/terraform-aws-sso-elevator/blob/main/docs/cli.md).

## Install

Prebuilt binaries only cover macOS and Linux (`.goreleaser.yaml` builds for `goos: [darwin, linux]`, and `install.sh` rejects any other OS). On Windows, use [Build from source](#build-from-source) instead.

### Homebrew (macOS only, recommended on macOS)

Homebrew casks — the format `elevator` ships as — are a macOS-only concept; Homebrew itself refuses to install one on Linux. Linux users should use the [install script](#install-script) below instead.

```bash
brew tap fivexl/homebrew-tap
brew install elevator
```

### Install script

```bash
curl -fsSL https://raw.githubusercontent.com/fivexl/terraform-aws-sso-elevator/main/install.sh | sh
```

Downloads the right binary for your OS/arch from GitHub Releases, verifies its checksum, and installs it to `~/.local/bin` (override with `ELEVATOR_INSTALL_DIR`). Pin a specific version with `ELEVATOR_VERSION=5.0.0`. Repository releases use stable bare `X.Y.Z` tags; the module and CLI always share the same version.

### Build from source

```bash
git clone https://github.com/fivexl/terraform-aws-sso-elevator.git
cd terraform-aws-sso-elevator/cmd/elevator
go build -ldflags "-X main.version=dev -X main.buildCommit=$(git rev-parse --short HEAD) -X main.buildDate=$(date -u +%Y-%m-%dT%H:%M:%SZ)" -o elevator .
```

This is a separate, nested Go module (`cmd/elevator/go.mod`) — the rest of this repo is Python/Terraform, so building the CLI doesn't touch or require anything else in the repo.

A plain `go build -o elevator .` (no `-ldflags`) still works, but leaves `main.version`/`main.buildCommit`/`main.buildDate` at their zero values, so `elevator version` reports `elevator dev (commit none, built unknown)` regardless of what was actually built — the `-ldflags` above are what `.goreleaser.yaml` sets for every published release, so a binary built this way reports something meaningful instead.

### Verifying a release

`install.sh` always verifies the downloaded archive's checksum against the release's `checksums.txt`. On macOS it also fails closed unless the binary has a valid Developer ID signature from Apple Team ID `T962D4K3Y7`. Published macOS binaries are notarized by Apple, so Homebrew does not bypass Gatekeeper or clear quarantine attributes.

Each release includes an SBOM (`*.sbom.json`, one per archive) and a [GitHub build provenance attestation](https://docs.github.com/en/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds) tying that exact archive back to the workflow run and commit that produced it — no separate signing key to fetch or trust, since it's backed by GitHub's own OIDC identity. Verify with the [`gh` CLI](https://cli.github.com/):

```bash
gh attestation verify elevator-linux-amd64.tar.gz \
  --repo fivexl/terraform-aws-sso-elevator \
  --signer-workflow fivexl/terraform-aws-sso-elevator/.github/workflows/cli-release.yml
```

`--owner fivexl` alone (accepting an attestation from *any* repo in the `fivexl` org) or `--repo` alone (accepting one from *any* workflow in this repo with permission to publish attestations) are both weaker than necessary here — `--signer-workflow` pins verification to the one workflow that actually publishes this release, `cli-release.yml`, the same check `install.sh` itself runs.

Inspect the Apple identity on a downloaded macOS binary with:

```bash
codesign --verify --strict --verbose=2 elevator
codesign -dv elevator 2>&1 | grep -E 'TeamIdentifier|Authority'
codesign --verify -R '=anchor apple generic and certificate leaf[subject.OU] = T962D4K3Y7' elevator
```

## Configure

`elevator` needs to know which API endpoint to call. In order of precedence (first one set wins):

1. `--endpoint URL` flag, passed on any individual call
2. `ELEVATOR_ENDPOINT` environment variable — set this for scripts, CI, or an AI agent that can't run an interactive setup step
3. A saved config file, written once via:
   ```bash
   elevator configure --endpoint https://<api-id>.execute-api.<region>.amazonaws.com/default/access-requester-cli
   ```
   (writes `~/.elevator/config.json`)

Use the module's `requester_api_endpoint_url_cli` output as the endpoint.

**Custom domain.** The identity proof names the module's REST API id, and a default `execute-api` URL already contains it. If you reach the API through a custom domain, also set the id from the module's `requester_api_id` output. It resolves the same way: `--api-id` flag, then `ELEVATOR_API_ID`, then the saved config:

```bash
elevator configure --endpoint https://elevator.example.com/access-requester-cli --api-id abcde12345
```

`configure --endpoint` clears a saved API id, because that id belonged to the old endpoint; `configure --api-id` alone keeps the saved endpoint. A configured API id is ignored when the endpoint is an `execute-api` URL, which already names the API. The region is not saved: with a custom domain, the signing region comes from `--region`, `AWS_REGION` or your profile's region, and must be the deployment's region. Otherwise API Gateway's `AWS_IAM` check rejects the request with `403` ("Credential should be scoped to a valid region").

Credentials and region come from the standard AWS SDK chain — `AWS_PROFILE`, `AWS_REGION`, an active SSO session, etc. — the same way any AWS CLI command resolves them. `elevator` doesn't have its own profile setting; there's nothing extra to configure for auth beyond a normal AWS environment.

**Upgrading to 5.0.0.** 5.0.0 moved the module to a new API, so the old endpoint is gone and older CLIs are rejected. Install CLI 5.0.0 or later and configure the new endpoint; see [UPGRADE-5.0.md](https://github.com/fivexl/terraform-aws-sso-elevator/blob/main/UPGRADE-5.0.md).

## Use

```bash
elevator --account 123456789012 --permission-set ReadOnly --duration 120 --reason "debugging prod issue"
```

- `--account` — AWS account ID to request access to (required)
- `--permission-set` — Permission set name to request (required)
- `--duration` — How long access is needed, as a positive integer number of minutes (required). Any whole number of minutes is valid, up to whatever maximum this deployment allows — not limited to the specific options the Slack request modal's dropdown shows.
- `--reason` — Reason for the access request (required, at most 1000 characters)
- `--endpoint` — SSO Elevator API invoke URL for this call only, overriding `ELEVATOR_ENDPOINT` and the saved config file (see [Configure](#configure))
- `--api-id` — REST API id for this call only, needed only with a custom-domain endpoint; overrides `ELEVATOR_API_ID` and the saved config file
- `--region` — AWS region for SigV4 signing; if omitted, it's parsed from `--endpoint`'s own hostname when that's a standard `execute-api.<region>.amazonaws.com` URL, else the resolved AWS config region, falling back to `us-east-1`

Run `elevator --help` for the full flag reference.

**What a successful submission means — and doesn't mean.** A `2xx` status alone isn't the whole signal: the server can refuse a request — no approvers configured for that account/permission-set combination, the caller isn't allowed to request it, and similar policy reasons — with `200` and an explicit `{"ok": false, ...}` body, not a `4xx`/`5xx`. `elevator` checks that `ok` field itself and exits non-zero with `request was not submitted: ...` when it's `false`, so an exit code of `0` is what actually means the request was received and posted into the approval workflow — not the raw HTTP status alone. Even a genuine `0`-exit submission does **not** mean access has been granted: depending on the module's configuration, the request may be granted automatically (if you're a self-approving approver for that account/permission-set combination) or may require someone else to click Approve/Deny in Slack — and the response looks the same either way, since the server can't tell your specific case apart from the response alone. `elevator` does not poll or wait for the final decision; check Slack, or the account's IAM Identity Center assignments, to confirm the actual outcome.

**Timeouts and retries.** Each attempt is bounded to 35 seconds. A connection that fails before reaching the server (DNS, refused connection) is retried automatically up to 3 times; a timeout waiting for a response is not retried automatically, since the request may already have reached the Lambda by then — retrying blindly could submit (and possibly auto-grant) the same request twice. If you hit that, check Slack or IAM Identity Center before running the command again.

Check the installed version and build info with `elevator version`.

## How your identity is proved

API Gateway checks your request signature, but the Lambda behind it cannot trust what API Gateway tells it about you: anyone allowed to invoke the Lambda directly could make up that information. So `elevator` also sends a presigned `sts:GetCallerIdentity` request inside the request body. It contains:

- a URL for your region's STS endpoint, signed with your credentials, including your session token for temporary credentials. Your secret access key is never sent.
- signed headers binding it to this request: the SHA-256 of the request body, the module's REST API id, and a random nonce.

The Lambda checks the binding, sends the presigned request to STS once, and uses the ARN and account STS returns as your identity. A proof is accepted for 60 seconds and only for this exact request on this deployment.

What this means for you:

- Your credentials must be able to call `sts:GetCallerIdentity`. Any valid AWS credentials can: it needs no IAM permission, and an explicit deny does not block it.
- Your clock must be accurate. A clock more than 30 seconds fast or about 60 seconds slow gets `403`.
- `503` means STS or AWS Organizations did not answer in time. Run the command again.
