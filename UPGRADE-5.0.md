# Upgrade to 5.0

## Upgrade from 5.0.x

[SnapStart](#snapstart) is on by default (`snap_start = true`).

1. Where Lambda doesn't offer SnapStart for your region and package type, set `snap_start = false` (see `snap_start` in [Inputs](#inputs)).
2. Run `terraform apply`. It publishes a SnapStart version of the requester and moves `live` to it; this apply takes a few extra minutes while Lambda takes the snapshot.
3. The revoker's first nightly run deletes the requester versions published by 5.0.x (see [SnapStart](#snapstart)).

## Upgrade to 5.0.0

5.0.0 replaces the HTTP API with one REST API that serves both Slack and the CLI (see [API Gateway](#api-gateway)). The apply that creates the new API destroys the old ones, so the Slack and CLI URLs both change. Slack and the CLI are down from that apply until you finish the steps after it. There is no zero-downtime path, so pick a quiet window.

Before the apply:

1. Follow steps 1–4 of [Upgrade from v4](#upgrade-from-v4): Terraform and provider versions, and moving the Slack secrets to SSM. Slack keeps working through these steps.
2. Remove the other inputs 5.0.0 no longer has. Terraform fails with "Unsupported argument" on each:
   - `create_api_gateway`: the API is always created. If you set it to `false` and fronted the Lambda with your own API, that setup is no longer supported.
   - `create_lambda_url`: the Lambda Function URL and the `lambda_function_url` output are gone. `create_lambda_url` defaulted to `true`, so a deployment that never set it still had a Function URL; anything pointing at it stops working.
   - `cli_sso_role_name_prefix`.
   - `event_brige_check_on_inconsistency_rule_name` and `event_brige_scheduled_revocation_rule_name`: renamed to `event_bridge_*`.
3. The CLI route is now on by default. Its resource policy is built from the organization id, so the principal running Terraform needs Organizations read permissions (see [CLI tool](#cli-tool)), and the account must belong to an AWS Organization. If you only use Slack, set `enable_access_requester_cli = false` instead.
4. The HTTP API had stage access logs on, in a log group the module created. The apply deletes that log group with the HTTP API; export it first if you need the history. The REST API logs nothing by default: set `api_gateway_access_logs_enabled = true` to log again, which needs the account's API Gateway CloudWatch Logs role (see [API Gateway](#api-gateway)).
5. To use WAF, set `waf_enabled = true` or `waf_web_acl_arn` now (see [AWS WAF](#aws-waf)). If AWS Firewall Manager attaches a web ACL to your APIs, leave both unset.

The apply:

6. Run the full `terraform apply` (step 5 of [Upgrade from v4](#upgrade-from-v4)). Downtime starts here.

Right after the apply:

7. Slack: in the Slack app settings, set the Interactivity Request URL (`request_url` in the manifest) to the `requester_api_endpoint_url` output.
8. CLI users: upgrade the `elevator` CLI to 5.0.0. The Lambda answers older CLIs with `400` and "This SSO Elevator deployment requires elevator CLI 5.0.0 or newer". Then re-run `elevator configure --endpoint` with the `requester_api_endpoint_url_cli` output. If the CLI reaches the API through a custom domain, also run `elevator configure --api-id` with the `requester_api_id` output. Update `ELEVATOR_ENDPOINT` (and set `ELEVATOR_API_ID` for a custom domain) wherever scripts or CI set it, since the environment overrides the saved config.
9. Cross-account CLI callers: their permission sets need `execute-api:Invoke` on the new `requester_api_execution_arn_cli` output. The old ARN names a deleted API.
10. Delete the requester Lambda's published versions from 4.x. Each one keeps its own code and settings and stays invocable as `function:<name>:<N>` by anyone allowed `lambda:InvokeFunction` on it. If the CLI route was on in 4.x, those versions trust the caller identity in the event, which a direct invoke can forge. Delete every version older than the one the `live` alias points to:

   ```sh
   FN=access-requester   # your requester_lambda_name
   LIVE=$(aws lambda get-alias --function-name "$FN" --name live --query FunctionVersion --output text)
   for v in $(aws lambda list-versions-by-function --function-name "$FN" --query 'Versions[].Version' --output text); do
     if [ "$v" != '$LATEST' ] && [ "$v" -lt "$LIVE" ]; then
       aws lambda delete-function --function-name "$FN" --qualifier "$v"
     fi
   done
   ```

11. Monitoring: a POST to the Slack URL without the `X-Slack-Signature` and `X-Slack-Request-Timestamp` headers now gets `400` from API Gateway, without invoking the Lambda. Before, the Lambda answered `401`. Update uptime checks and alerts that expect the old code.

## Upgrade from v4

These are the secrets steps of [Upgrade to 5.0.0](#upgrade-to-500); follow that section, which tells you when to come here. The v4 Lambdas keep their secrets in environment variables until step 5, so Slack keeps working through step 4. Step 5 also replaces the API, which starts the downtime.

1. In the root module, require Terraform >= 1.11 and hashicorp/aws >= 6.28 (see the provider's [version 6 upgrade guide](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/guides/version-6-upgrade) for your other resources). Remove `slack_bot_token` and `slack_signing_secret` from the module block, along with any `aws_ssm_parameter` data sources that fed them, tfvars entries, `-var` flags and `TF_VAR_*` variables in CI. Bump the module version and run `terraform init -upgrade`.
2. If parameters already exist at the default names (the v4 example had you create both by hand), save their values, then delete them so the module can create its own:

   ```sh
   aws ssm get-parameter --with-decryption --name /sso-elevator/slack-bot-token --query Parameter.Value --output text
   aws ssm get-parameter --with-decryption --name /sso-elevator/slack-signing-secret --query Parameter.Value --output text
   aws ssm delete-parameter --name /sso-elevator/slack-bot-token
   aws ssm delete-parameter --name /sso-elevator/slack-signing-secret
   ```

   If a parameter was already overwritten, `aws ssm get-parameter-history --with-decryption --name <name>` shows earlier values. You can also copy both secrets from the Slack app settings instead.

   Don't `terraform import` the existing parameters instead. With no write-only value to go on, the import stores the decrypted secret in state. The next apply then writes `REPLACE_ME` over the real value. That apply does clear the secret from the current state, but the earlier state version still holds it, so you'd still be putting the values back by hand. Both behaviours were confirmed against hashicorp/aws 6.67.0.
3. Create only the two parameters:

   ```sh
   terraform apply \
     -target='module.aws_sso_elevator.aws_ssm_parameter.slack_bot_token' \
     -target='module.aws_sso_elevator.aws_ssm_parameter.slack_signing_secret'
   ```

4. Write both secrets with the `put-parameter` commands above.
5. Run a full `terraform apply`. This switches the Lambdas to the v5 image and to SSM in one step. If you pin `ecr_repo_tag` or host images yourself (`ecr_repo_name`/`ecr_owner_account_id`), point it at a 5.x image first: a v4 image cannot read the secrets from SSM and fails at cold start. Then continue with step 7 of [Upgrade to 5.0.0](#upgrade-to-500).

Terraform state from v4 still holds both secrets, in the Lambda environment variables and in any `aws_ssm_parameter` data source values. After upgrading, rotate both secrets in Slack, and purge old state versions from your backend if that matters to you.
