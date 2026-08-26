# elevate

Submit a temporary AWS access request without Slack.

## Why this exists

The normal SSO Elevator flow happens entirely in Slack: you post a request, an approver clicks Approve, and the module grants a temporary permission set. That works well for a person, but it's awkward for a script, a CI job, or an AI coding agent that can't click a button — they need a command that submits the same request and reports a clear, machine-readable result. `elevate` signs the request with your own local AWS credentials and posts it directly to the module's `POST /access-requester-cli` route; API Gateway's `AWS_IAM` authorizer verifies that signature itself, and the Lambda extracts your identity from the verified request context (`src/cli_auth.py`) before running it through the exact same approval pipeline a Slack-submitted request goes through — same approvers, same self-approval rules, same audit log.

## Install

Homebrew and the install script below only support macOS and Linux (`.goreleaser.yaml` builds for `goos: [darwin, linux]`, and `install.sh` rejects any other OS). On Windows, use [Build from source](#build-from-source) instead.

### Homebrew (recommended)

```bash
brew tap fivexl/homebrew-tap
brew install elevate
```

### Install script

```bash
curl -fsSL https://raw.githubusercontent.com/fivexl/terraform-aws-sso-elevator/main/install.sh | sh
```

Downloads the right binary for your OS/arch from GitHub Releases, verifies its checksum, and installs it to `~/.local/bin` (override with `ELEVATE_INSTALL_DIR`). Pin a specific version with `ELEVATE_VERSION=v1.2.0`.

### Build from source

```bash
git clone https://github.com/fivexl/terraform-aws-sso-elevator.git
cd terraform-aws-sso-elevator/cmd/elevate
go build -o elevate .
```

This is a separate, nested Go module (`cmd/elevate/go.mod`) — the rest of this repo is Python/Terraform, so building the CLI doesn't touch or require anything else in the repo.

## Configure

`elevate` needs to know which API endpoint to call. In order of precedence (first one set wins):

1. `--endpoint URL` flag, passed on any individual call
2. `ELEVATE_ENDPOINT` environment variable — set this for scripts, CI, or an AI agent that can't run an interactive setup step
3. A saved config file, written once via:
   ```bash
   elevate configure --endpoint https://<api-id>.execute-api.<region>.amazonaws.com/default/access-requester-cli
   ```
   (writes `~/.sso-elevator-cli/config.json`)

Credentials and region come from the standard AWS SDK chain — `AWS_PROFILE`, `AWS_REGION`, an active SSO session, etc. — the same way any AWS CLI command resolves them. `elevate` doesn't have its own profile setting; there's nothing extra to configure for auth beyond a normal AWS environment.

## Use

```bash
elevate --account 123456789012 --permission-set ReadOnly --duration 2 --reason "debugging prod issue"
```

- `--account` — AWS account ID to request access to (required)
- `--permission-set` — Permission set name to request (required)
- `--duration` — How long access is needed, as a positive integer number of hours (required)
- `--reason` — Reason for the access request (required)
- `--endpoint` — SSO Elevator API invoke URL for this call only, overriding `ELEVATE_ENDPOINT` and the saved config file (see [Configure](#configure))
- `--region` — AWS region for SigV4 signing; defaults to the resolved AWS config region, falling back to `us-east-1`

Run `elevate --help` for the full flag reference.

**What a successful submission means — and doesn't mean.** A `2xx` response means the request was received and posted into the approval workflow; it does **not** mean access has been granted. Depending on the module's configuration, the request may be granted automatically (if you're a self-approving approver for that account/permission-set combination) or may require someone else to click Approve/Deny in Slack — and the response looks the same either way, since the server can't tell your specific case apart from the response alone. `elevate` does not poll or wait for the final decision; check Slack, or the account's IAM Identity Center assignments, to confirm the actual outcome.

Check the installed version and build info with `elevate version`.
