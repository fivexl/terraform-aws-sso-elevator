---
name: request-aws-access
description: Request temporary AWS access through SSO Elevator with the `elevator` CLI. Use when an AWS call fails with AccessDenied or the task needs a permission set the user does not hold, and the organization runs SSO Elevator. Also use when the user asks to "elevate", "request access", or run `elevator`.
---

# Request AWS Access with `elevator`

`elevator` submits the same request as the SSO Elevator Slack form. It is an
access request on the user's behalf, seen by their approvers in Slack, and it
can grant access immediately when the user self-approves. Treat it like any
outward-facing action: **show the exact command and get the user's yes before
every run.** Never submit a request the user did not agree to, and never
retry one on your own.

## 1. Check the Preconditions

Run these before building a request. Stop and tell the user at the first one
that fails; the fix is theirs.

```bash
elevator version                     # missing → https://github.com/fivexl/terraform-aws-sso-elevator/blob/main/cmd/elevator/README.md#install
aws sts get-caller-identity          # with the profile you will sign with
```

- The caller ARN must be an `assumed-role/AWSReservedSSO_...` session. IAM
  users, other roles and CI/OIDC roles are rejected. If the SSO session has
  expired, ask the user to log in; do not try another credential source.
- That session must come from a standing permission set, not from access
  `elevator` granted earlier.
- An endpoint must be set: `ELEVATOR_ENDPOINT`, or `~/.elevator/config.json`
  from `elevator configure`. If neither exists, ask the user for the
  `requester_api_endpoint_url_cli` value; do not guess a URL.
- The CLI version must match the module's major version. A `400` asking to
  upgrade means the CLI is too old.

## 2. Build the Request with the User

All four flags are required. Fill none of them by guessing.

| Flag | Rule |
|---|---|
| `--account` | 12-digit account id. Take it from the failing call or ask. |
| `--permission-set` | Exact permission set name. Propose the least-privileged set that does the task (read-only for reading). |
| `--duration` | Minutes. Propose the shortest that covers the task, often 30–60. The deployment caps it; above the cap returns `400`. |
| `--reason` | What the user is doing and why, in their words, at most 1000 characters. Approvers read it. No secrets, tokens or customer data. |

Show the full command and wait for approval:

```bash
elevator --account 123456789012 --permission-set ReadOnly --duration 60 \
  --reason "Investigate failing deploy of service X (ticket ABC-123)"
```

Pass `AWS_PROFILE=<profile>` on the command when the default profile is not
the SSO session to sign with.

## 3. Read the Result

Exit code `0` means the request reached the approval workflow. It does
**not** mean access was granted, and the output is the same whether it was
granted automatically or waits for a human. Diagnostics go to stderr; the
`✓ Request submitted.` summary goes to stdout.

| Result | Meaning | Do |
|---|---|---|
| exit 0 | Submitted | Tell the user it is pending or self-approved; go to step 4. |
| `request was not submitted: ...` | Server refused it, for example no approvers for that account and permission set | Relay the message. Do not retry. |
| `400` | Bad input (unknown permission set, account outside the org, duration over the cap) or CLI too old | Fix the input with the user. |
| `403` | Not an SSO session, session name not an Identity Store user, no Slack user for that email, caller outside the org, missing `execute-api:Invoke`, or clock skew over 30 s | Relay the body; the user or operator must fix it. Do not retry. |
| `503` | STS or Organizations timed out | Safe to run once more, with the user's okay. |
| `500` or timeout | Unknown: the request may already be posted in Slack | Do **not** retry. Ask the user to check Slack. |
| `... never established ... safe to retry` | Connection failed before sending | Safe to run once more. |

## 4. Confirm Access Before Using It

`elevator` does not wait for the decision. Ask the user to tell you when it is
approved, or check that the access works, for example with an AWS profile set
to that account and permission set:

```bash
aws sts get-caller-identity --profile <profile-for-that-account-and-permission-set>
```

Do not poll in a tight loop, and do not submit a second request because the
first one is still pending. Access ends on its own when the duration runs
out; do not request an extension without asking.
