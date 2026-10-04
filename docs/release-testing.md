# Release testing

Live checks to run on real AWS before a release that changes the Lambdas, the API
Gateway, WAF, IAM or the Slack secret handling. Unit tests mock AWS and Slack, so they
cannot catch IAM gaps, API Gateway behaviour, drift or botocore differences between
`src/uv.lock` and the image's `layer/uv.lock`.

Record each run in the PR's test plan: commit, image tag and digest, `elevator version`
output, scenario, result.

## Setup

- An AWS Organization with IAM Identity Center, a Slack app and a test channel.
- Two deployments, because a delegated administrator cannot manage access to the
  management account ([SSO delegation](docs.md#sso-delegation)):
  - **tooling**: the module in the delegated administrator account (the CLI route is on by
    default);
  - **management**: the module in the management account, approval statements scoped
    to the management account only. Otherwise its revoker removes user-level
    assignments that the tooling deployment made in other accounts.
- Pull request images are tagged `pr-<number>-<short sha>`: set `ecr_repo_tag` to that tag
  and point the module `source` at the same commit.
- The `elevator` CLI from the latest release, unless the change touches `cmd/elevator`.
- Each CLI request: a low-privilege permission set, 15 minutes, a reason naming the test.

## Scenarios

### Install and upgrade

1. **Fresh install.** Apply into an account without the module.
   - Expect: both SSM parameters hold `REPLACE_ME`, and an access-requester invoke fails at
     init with an error naming the parameter.
   - Write the real secrets (README "Fresh install"). Without a redeploy, the next request
     succeeds.
2. **Upgrade from the previous major.** Follow the README upgrade section step by step on a
   deployment running the previous release.
   - Expect: the secret hashes match before and after; the Lambda environment holds parameter
     names, not values. Whether Slack keeps working throughout depends on the release: see its
     upgrade section (5.0.0 has downtime).
3. **No drift.** Run `terraform plan -detailed-exitcode` right after each apply.
   - Expect: exit code 0.

### Requests

4. **Slack request**, approved in Slack.
   - Expect: assignment created, revocation scheduled.
5. **CLI, deployment account**: an SSO session in the account the module runs in.
   - Expect: `Request submitted`, assignment created.
6. **CLI, other org account, least privilege**: an SSO session whose permission set has
   only `execute-api:Invoke` on the `requester_api_execution_arn_cli` output.
   - Expect: `Request submitted`, assignment created.
   - The same caller under a permission set without that action gets `403` from API Gateway.
7. **CLI, outside the organization**: an SSO session in an account outside the org.
   - Expect: `403` from API Gateway, "no resource-based policy allows"; the Lambda does not run.
8. **CLI, not an SSO session**: an IAM role in an org account that has `execute-api:Invoke`,
   assumed with the session name set to a real Identity Store username.
   - Expect: `403` with "not associated with an SSO session" from the Lambda; no assignment.
9. **Management account assignment**: a request to the management account through the
   management deployment.
   - Expect: assignment created, revoked on expiry.

### Background Lambdas and failure handling

10. **Revoker**: wait for the grants from 4–9 to expire.
    - Expect: each assignment removed; no errors in the revoker log.
11. **Attribute syncer**: invoke with `{}`.
    - Expect: `success: true`, `error_count: 0`, no warnings.
12. **Placeholder secret on a running deployment**: save both values, write `REPLACE_ME`
    to both, force new containers on the `live` alias (README "Rotating a secret"), then:
    - invoke the access-requester: expect init failure;
    - invoke the attribute syncer: expect success with an error logged for the Slack token.

    Restore the values, check their hashes, force new containers again, and repeat a
    CLI request: expect success.

## 5.0.0: REST API, WAF and CLI identity proof

Run on top of the scenarios above, on the release commit. Tick each box in the PR's test plan
with its evidence.

Upgrade and install:

- [ ] Fresh install, then upgrade a 4.4.x deployment by following README "Upgrade to 5.0.0".
      Expect: Slack and the CLI work again once steps 7–9 are done; old versions deleted by
      step 10.
- [ ] Slack: the access shortcut opens the modal, submitting it posts the request, and Approve
      and Deny both work (lazy listeners invoked through the `live` alias).
- [ ] A POST to `requester_api_endpoint_url` without Slack headers gets `400` and the Lambda's
      `Invocations` metric does not move. Real Slack requests pass the validator. If they do
      not (header case), stop: do not fall back silently.
- [ ] A second apply after a code change moves `live` to the new version, and requests run on
      it (the log stream names the new version).
- [ ] Management-account deployment: the first grant into the management account succeeds.

CLI:

- [ ] Same-account and cross-account requests succeed.
- [ ] Rejected proofs: tampered payload, a proof older than 60 seconds, a proof for another API
      id, and a 4.x CLI. Expect `403`, `403`, `403`, and `400` asking to upgrade.
- [ ] A direct `aws lambda invoke` with a CLI-shaped event naming another user is rejected,
      with the CLI route on and with it off.
- [ ] `enable_access_requester_cli` on, then off, then on: each apply is followed by a clean
      `terraform plan -detailed-exitcode`. After turning it off, the stage may still route
      `POST /access-requester-cli` until the next redeploy; expect `403` or `500`, with the Lambda
      rejecting it for lack of `CLI_EXPECTED_API_ID`.
      `terraform apply -replace=aws_api_gateway_deployment.requester` removes the route.

WAF:

- [ ] `waf_enabled = true`, then `waf_web_acl_arn` with your own web ACL, then neither: each
      apply is followed by a clean plan. Setting both fails at plan.
- [ ] With the module web ACL, a Slack request and a CLI request both pass.

Revoker and retries:

- [ ] A scheduled revocation runs from its one-time schedule, not the daily
      `schedule_expression` run. Group revocation, approver reminders and request expiry
      schedules also fire.
- [ ] A forced lazy-listener failure (for example, a permission removed for the test) runs
      once, with no retry.
