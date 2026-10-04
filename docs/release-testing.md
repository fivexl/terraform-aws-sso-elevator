# Release testing

Live checks to run on real AWS before a release that changes the Lambdas, the API
gateways, IAM or the Slack secret handling. Unit tests mock AWS and Slack, so they
cannot catch IAM gaps, API Gateway behaviour, drift or botocore differences between
`src/uv.lock` and the image's `layer/uv.lock`.

Record each run in the PR's test plan: image tag, scenario, result.

## Setup

- An AWS Organization with IAM Identity Center, a Slack app and a test channel.
- Two deployments, because a delegated administrator cannot manage access to the
  management account ([SSO delegation](docs.md#sso-delegation)):
  - **tooling**: the module in the delegated administrator account, with
    `enable_access_requester_cli = true`;
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
   - Expect: Slack keeps working throughout; the secret hashes match before and after; the
     Lambda environment holds parameter names, not values.
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
    to both, force new containers (`update-function-configuration --description ...`), then:
    - invoke the access-requester: expect init failure;
    - invoke the attribute syncer: expect success with an error logged for the Slack token.

    Restore the values, check their hashes, force new containers again, and repeat a
    CLI request: expect success.
