---
name: request-aws-access
description: Request temporary AWS access through SSO Elevator with the `elevator` CLI. Use when an AWS call fails with AccessDenied or the task needs a permission set the user does not hold, and the organization runs SSO Elevator. Also use when the user asks to submit an SSO Elevator access request or to run `elevator` to get access.
---

# Request AWS Access with `elevator`

`elevator` submits the same request as the SSO Elevator Slack form. It is an
access request on the user's behalf, seen by their approvers in Slack, and it
can grant access immediately when the user self-approves. Treat it like any
outward-facing action: **show the exact command and get the user's yes before
every run, including a retry.** Never submit a request the user did not agree
to.

## 1. Check the Preconditions

Run these before building a request. Stop and tell the user at the first one
that fails; the fix is theirs.

```bash
elevator version                     # missing → https://github.com/fivexl/terraform-aws-sso-elevator/blob/main/cmd/elevator/README.md#install
aws sts get-caller-identity          # with the profile you will sign with
```

- The CLI must be 5.0.0 or newer. An older one gets a `400` asking to
  upgrade.
- The caller ARN must be an `assumed-role/AWSReservedSSO_...` session. IAM
  users, other roles and CI/OIDC roles are rejected. If the SSO session has
  expired, ask the user to log in; do not try another credential source.
- Sign with the user's usual SSO profile. A profile that only works because of
  an earlier `elevator` grant cannot sign requests, and the ARN does not show
  the difference, so ask the user when unsure.
- An endpoint must be set: `ELEVATOR_ENDPOINT`, or `~/.elevator/config.json`
  from `elevator configure`. If neither exists, ask the user for the
  `requester_api_endpoint_url_cli` value; do not guess a URL. A custom-domain
  endpoint also needs the API id (`ELEVATOR_API_ID` or `--api-id`).
- The user's Identity Store primary email must belong to a Slack user in the
  workspace. You cannot check this; a `403` may mean it does not.

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
granted automatically or waits for a human. Diagnostics, including
`Status: <code>` and the response body, go to stderr; the
`✓ Request submitted.` summary goes to stdout.

| Result | Meaning | Do |
|---|---|---|
| exit 0 | Submitted | Tell the user it is pending or self-approved; go to step 4. |
| `request was not submitted: ...` | Server refused it, for example no approvers for that account and permission set | Relay the message. Do not retry. |
| `400` | Bad input: an account or permission set this deployment does not offer, duration over the cap, reason too long, or a CLI older than 5.0.0 | Relay the message and fix the input with the user. |
| `403` | Identity check failed. One generic message covers: not an SSO session, session name not an Identity Store user, no Slack user for that email, caller account outside the organization, wrong API id, or a clock more than 30 s fast or about 60 s slow. A missing `execute-api:Invoke` also gets `403`, from API Gateway. | Relay the body; the user or operator must fix it. Do not retry. |
| `503` | Transient AWS error while verifying the request | Safe to run once more, with the user's okay. |
| `500` or timeout | Unknown: the request may already be posted in Slack | Do **not** retry. Ask the user to check Slack. |
| `... never established ... safe to retry` | Connection failed before sending | Safe to run once more, with the user's okay. |

## 4. Confirm Access Before Using It

`elevator` does not wait for the decision. Ask the user to tell you when it is
approved. Then check once that the assignment exists, with an AWS profile set
to that account and permission set:

```bash
aws sts get-caller-identity --profile <profile-for-that-account-and-permission-set>
```

This shows only that the role can be assumed. To confirm the permissions,
rerun the call that failed.

Do not poll, and do not submit a second request because the first one is
still pending. Access ends on its own when the duration runs out; do not
request an extension without asking.
