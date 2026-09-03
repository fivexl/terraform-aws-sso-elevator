# elevator

Submit a temporary AWS access request without Slack.

## Why this exists

The normal SSO Elevator flow happens entirely in Slack: you post a request, an approver clicks Approve, and the module grants a temporary permission set. That works well for a person, but it's awkward for a script, a CI job, or an AI coding agent that can't click a button — they need a command that submits the same request and reports a clear result via its exit code (0 on success, non-zero otherwise) and human-readable output on stdout/stderr. There's no `--json` / structured-output mode yet, so a caller that needs to parse the result programmatically (rather than just check the exit code) has to parse this prose output itself. `elevator` signs the request with your own local AWS credentials and posts it directly to the module's `POST /access-requester-cli` route; API Gateway's `AWS_IAM` authorizer verifies that signature itself, and the Lambda extracts your identity from the verified request context (`src/cli_auth.py`) before running it through the exact same approval pipeline a Slack-submitted request goes through — same approvers, same self-approval rules, same audit log.

Your AWS identity also needs `execute-api:Invoke` permission on this route in the account the module is deployed into — typical member-account SSO credentials (e.g. a plain `ReadOnly` session in a different account) will not have it, and you'll see a `403` if it's missing. Check with whoever manages your SSO permission sets if you're not sure you have it.

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

Downloads the right binary for your OS/arch from GitHub Releases, verifies its checksum, and installs it to `~/.local/bin` (override with `ELEVATOR_INSTALL_DIR`). Pin a specific version with `ELEVATOR_VERSION=elevator-v1.2.0`.

### Build from source

```bash
git clone https://github.com/fivexl/terraform-aws-sso-elevator.git
cd terraform-aws-sso-elevator/cmd/elevator
go build -o elevator .
```

This is a separate, nested Go module (`cmd/elevator/go.mod`) — the rest of this repo is Python/Terraform, so building the CLI doesn't touch or require anything else in the repo.

### Verifying a release

`install.sh` always verifies the downloaded archive's checksum against the release's `checksums.txt` before installing anything, so this section is only for confirming the release itself is genuinely what this repo's CI built — useful if you're distributing the binary further, or just want stronger assurance than "GitHub says so."

Each release includes an SBOM (`*.sbom.json`, one per archive) and a [GitHub build provenance attestation](https://docs.github.com/en/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds) tying that exact archive back to the workflow run and commit that produced it — no separate signing key to fetch or trust, since it's backed by GitHub's own OIDC identity. Verify with the [`gh` CLI](https://cli.github.com/):

```bash
gh attestation verify elevator-linux-amd64.tar.gz --owner fivexl
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

Credentials and region come from the standard AWS SDK chain — `AWS_PROFILE`, `AWS_REGION`, an active SSO session, etc. — the same way any AWS CLI command resolves them. `elevator` doesn't have its own profile setting; there's nothing extra to configure for auth beyond a normal AWS environment.

## Use

```bash
elevator --account 123456789012 --permission-set ReadOnly --duration 120 --reason "debugging prod issue"
```

- `--account` — AWS account ID to request access to (required)
- `--permission-set` — Permission set name to request (required)
- `--duration` — How long access is needed, as a positive integer number of minutes (required). Any whole number of minutes is valid, up to whatever maximum this deployment allows — not limited to the specific options the Slack request modal's dropdown shows.
- `--reason` — Reason for the access request (required)
- `--endpoint` — SSO Elevator API invoke URL for this call only, overriding `ELEVATOR_ENDPOINT` and the saved config file (see [Configure](#configure))
- `--region` — AWS region for SigV4 signing; if omitted, it's parsed from `--endpoint`'s own hostname when that's a standard `execute-api.<region>.amazonaws.com` URL, else the resolved AWS config region, falling back to `us-east-1`

Run `elevator --help` for the full flag reference.

**What a successful submission means — and doesn't mean.** A `2xx` response means the request was received and posted into the approval workflow; it does **not** mean access has been granted. Depending on the module's configuration, the request may be granted automatically (if you're a self-approving approver for that account/permission-set combination) or may require someone else to click Approve/Deny in Slack — and the response looks the same either way, since the server can't tell your specific case apart from the response alone. `elevator` does not poll or wait for the final decision; check Slack, or the account's IAM Identity Center assignments, to confirm the actual outcome.

**Timeouts and retries.** Each attempt is bounded to 35 seconds. A connection that fails before reaching the server (DNS, refused connection) is retried automatically up to 3 times; a timeout waiting for a response is not retried automatically, since the request may already have reached the Lambda by then — retrying blindly could submit (and possibly auto-grant) the same request twice. If you hit that, check Slack or IAM Identity Center before running the command again.

Check the installed version and build info with `elevator version`.
