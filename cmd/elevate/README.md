# elevate — SSO Elevator CLI

A small Go CLI that submits a temporary access request without going through Slack. It signs the request directly with the caller's own local AWS credentials and posts it to the module's `POST /access-requester-cli` route; API Gateway's `AWS_IAM` authorizer verifies that signature itself, and the Lambda extracts the caller's identity from the verified request context (see `src/cli_auth.py`) before running it through the same approval pipeline as a Slack-submitted request.

Originally prototyped in the `sso-elevator-cli-e2e` spike repo (kept there as historical reference); this is now the canonical version, developed alongside the server-side integration code it talks to (`src/main.py`, `src/cli_auth.py`) so both sides change together.

This is a separate, nested Go module (`cmd/elevate/go.mod`) — the rest of this repo is Python/Terraform, so Go tooling here is self-contained and doesn't affect anything else.

## Building

```bash
cd cmd/elevate
go build -o elevate .
```

## Configuring

Save the endpoint once so it doesn't need to be passed on every call:

```bash
./elevate configure --endpoint https://<api-id>.execute-api.<region>.amazonaws.com/default/access-requester-cli
```

This writes `~/.sso-elevator-cli/config.json`. `--endpoint` can still be passed on any individual call to override the saved config for that one call.

## Requesting access

```bash
./elevate \
  --account 123456789012 \
  --permission-set ReadOnly \
  --duration 2 \
  --reason "debugging prod issue"
```

`--duration` is a positive integer number of hours. The CLI resolves credentials and region from the standard AWS SDK chain (`AWS_PROFILE`, `AWS_REGION`, SSO session, etc.) — no profile is hardcoded; `--region` overrides the resolved region if needed (defaults to the resolved AWS config region, falling back to `us-east-1`).

On success, the CLI prints the HTTP response — for example `{"ok": true, "message": "Request received and posted for approval in Slack."}`. If the requester is an approver and self-approval is allowed for that resource/permission-set combination, the request is granted immediately; otherwise an approver sees it in Slack as usual.
