# CLI

People can also submit access requests from the command line, without Slack, with the `elevator` CLI. It posts to `POST /access-requester-cli`, signed with the caller's own AWS credentials, and the request goes through the same approval rules and audit log as a Slack request. This page is the operator side; for install and usage, see [`cmd/elevator/README.md`](../cmd/elevator/README.md).

The route is on by default (`enable_access_requester_cli = true`). Set it to `false` if you only use Slack: the route, its Lambda permission and the Organizations lookup are then not created, and the Lambda rejects CLI requests. When you turn an existing route off, the stage keeps serving it (API Gateway answers `500`) until you run `terraform apply -replace=module.<name>.aws_api_gateway_deployment.requester`.

Give users these module outputs:

- `requester_api_endpoint_url_cli`: the endpoint, for `elevator configure --endpoint` or `ELEVATOR_ENDPOINT`.
- `requester_api_id`: only if users reach the API through a custom domain. The identity proof names the REST API id, and a custom-domain URL does not contain it, so users also set it with `--api-id` or `ELEVATOR_API_ID`.
- `requester_api_execution_arn_cli`: for cross-account permission sets, below.

## Requirements

- The deployment account must belong to an AWS Organization: the API's resource policy is built from the organization id. To read the organization, the principal running Terraform needs `organizations:DescribeOrganization` for the `aws_organizations_organization` data source. That data source also calls `organizations:ListAccounts`, and outside the management account it ignores an access denied there. Where `ListAccounts` succeeds (the management account or a delegated administrator), it also needs `organizations:ListRoots` and `organizations:ListAWSServiceAccessForOrganization`.
- The resource policy (`aws:PrincipalOrgID`) admits callers from any account in the organization, and API Gateway rejects everyone else. Callers in the deployment account need nothing more. Callers in any other account also need `execute-api:Invoke` on the `requester_api_execution_arn_cli` output in their own identity policy (their permission set), as IAM requires for cross-account `AWS_IAM` calls:

  ```json
  {
    "Effect": "Allow",
    "Action": "execute-api:Invoke",
    "Resource": "arn:aws:execute-api:<region>:<deployment-account-id>:<requester_api_id>/default/POST/access-requester-cli"
  }
  ```

- Every CLI user needs a standing permission-set assignment somewhere in the organization, given outside SSO Elevator, to sign with. Access the elevator grants cannot bootstrap the CLI. Outside the deployment account, that permission set needs the statement above; the AWS managed `ReadOnlyAccess` policy does not include `execute-api:Invoke`.
- Callers must sign with an IAM Identity Center (SSO) session. IAM users, other roles, and CI/OIDC roles are rejected.
- The session name must be the caller's Identity Store username. IAM Identity Center sets it that way, so a normal `aws sso login` session qualifies. The Lambda matches it exactly (case-sensitive) against `UserName`, takes that user's primary email (or the first listed one), and looks up the Slack user with that email. If any step finds no match, the request is rejected with the same generic message as an invalid session. A username longer than 64 characters is truncated in the session name and therefore never matches.
- The requester Lambda needs outbound HTTPS to the regional STS endpoint, `sts.<region>.amazonaws.com`. It runs outside a VPC, so it has that by default. In an opt-in region the regional STS endpoint must be active; this has not been tested.
- If the module runs in an opt-in region, every caller's account must enable that region: the CLI's proof is signed for, and checked by, that region's STS endpoint.
- Only the standard `aws` partition is supported.

## Trust Model

API Gateway's `AWS_IAM` authorizer checks the request signature, and the resource policy limits callers to the organization. The Lambda cannot rely on that for identity: anyone allowed `lambda:InvokeFunction` on it can invoke it directly with an event naming any caller. So the CLI also sends a presigned `sts:GetCallerIdentity` request, signed with the same credentials and bound to the request body, the REST API id and a random nonce. The Lambda checks the binding and that the proof is at most 60 seconds old. It then sends the request to STS itself and takes the caller's ARN and account from STS's answer. The wire contract is in `src/cli_proof.py`.

The Lambda then checks, in `src/cli_auth.py`:

- the caller's account is in this organization (`organizations:DescribeAccount`; any error other than a clear "not found" fails the request);
- the assumed role's name starts with `AWSReservedSSO_`. IAM reserves this prefix in every account: `aws iam create-role --role-name AWSReservedSSO_ForgeTest_0000000000000000 ...` with administrator permissions fails with `InvalidInput: The role name 'AWSReservedSSO_ForgeTest_0000000000000000' is reserved for AWS use`. So the name proves the session comes from IAM Identity Center;
- the session name matches a real Identity Store user, as above.

An old CLI without a proof gets `400` asking to upgrade, an invalid proof gets the generic `403`, and a transient error from STS, Organizations or the Identity Store gets `503`. A proof can be replayed for about 90 seconds, re-submitting the identical request; see [accepted risks](accepted-risks.md#a-cli-proof-can-be-replayed-for-about-90-seconds).
