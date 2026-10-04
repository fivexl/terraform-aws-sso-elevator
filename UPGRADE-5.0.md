# Upgrading to 5.0

This guide takes a 4.x deployment to 5.0.0. For what is new, see the [changelog](CHANGELOG.md).

**On 3.x or older?** Upgrade to 4.4.3 first and check it works, then come back. [UPGRADE-4.0.md](UPGRADE-4.0.md) covers 3.x to 4.0. There is no tested path from 3.x straight to 5.0.

5.0.0 replaces the HTTP API with one REST API that serves Slack and the CLI. The full apply destroys the old API, so both URLs change, and Slack and the CLI are down until you finish the steps after the apply. Pick a quiet window.

**No rollback to 4.x.** Until the full apply you can stop at any step, and Slack keeps working on 4.x. After the full apply, fix forward.

## Breaking changes

| What changed | Who is affected | What to do |
| --- | --- | --- |
| Terraform `~> 1.0` → `~> 1.11`; hashicorp/aws `>= 4.64` → `>= 6.28`. | Everyone. | Raise both in the root module and run `terraform init -upgrade`. See the provider's [version 6 upgrade guide](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/guides/version-6-upgrade) for your other resources. |
| Inputs `slack_bot_token` and `slack_signing_secret` removed. The Slack secrets live in two SSM SecureString parameters you fill with the AWS CLI; they never pass through Terraform. | Everyone. | Remove both inputs and anything that fed them. The procedure below moves the secrets. See [docs/slack.md](docs/slack.md). |
| The module creates the SSM parameters `/sso-elevator/slack-bot-token` and `/sso-elevator/slack-signing-secret`. Creating one fails if a parameter of that name already exists. | Anyone with parameters at those names, for example from the 4.x example, which had you create them by hand. | Delete them during the upgrade, or set `slack_bot_token_ssm_parameter_name` and `slack_signing_secret_ssm_parameter_name` to new names. See the SSM check below. |
| The Lambdas read the Slack secrets from SSM; their environment holds only the parameter names. A 4.x image looks for the secrets in the environment and fails at cold start. | Anyone who pins `ecr_repo_tag` or hosts images (`ecr_repo_name`, `ecr_owner_account_id`). | Point at a 5.x image before the full apply. |
| Inputs `create_api_gateway`, `create_lambda_url`, `cli_sso_role_name_prefix`, `event_brige_check_on_inconsistency_rule_name` and `event_brige_scheduled_revocation_rule_name` removed. Output `lambda_function_url` removed. | Anyone who sets them. `create_lambda_url` defaulted to `true`, so every 4.x deployment that never set it had a Lambda Function URL. | Remove them; Terraform fails with "Unsupported argument" on each. The API is always created: a setup that fronted the Lambda with your own API is no longer supported. Anything calling the Function URL stops working. Rename `event_brige_*` to `event_bridge_*`; the defaults are the same names as before. |
| The HTTP API is destroyed and replaced by a REST API. Slack and CLI URLs change. | Everyone. | Update the Slack request URL and the CLI configuration after the apply. |
| The HTTP API's access-log group, created by the module, is deleted with it. REST API access logs are off by default. | Anyone who needs those logs. | Export the old log group before the apply. To log again, set `api_gateway_access_logs_enabled = true`. That needs the account's API Gateway CloudWatch Logs role ([`aws_api_gateway_account`](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/api_gateway_account)) set already; the module does not manage it. |
| The CLI route is on by default (`enable_access_requester_cli` `false` → `true`). Its resource policy is built from the organization id. | Everyone. | The account must belong to an AWS Organization, and the principal running Terraform needs Organizations read permissions ([docs/cli.md](docs/cli.md)). If you only use Slack, set `enable_access_requester_cli = false`. |
| CLI callers widen from the deployment account to every account in the organization. In 4.x the Lambda rejected callers from other accounts. | Anyone with the CLI route on. | Review your approval rules, especially statements that auto-approve: any user with an SSO session in any organization account can now submit CLI requests under them. Cross-account callers still need `execute-api:Invoke` in their permission set. |
| The CLI's execute-api ARN changes. The old ARN names a deleted API. | Anyone whose IAM policies grant `execute-api:Invoke` on the old ARN. | Point those policies at the `requester_api_execution_arn_cli` output after the apply. |
| The CLI must be 5.0.0 or newer: it sends an STS identity proof. Custom domains need the REST API id. | Every CLI user. | Upgrade the CLI and re-run `elevator configure`. Behind a custom domain, also set `--api-id` or `ELEVATOR_API_ID` from the `requester_api_id` output. An older CLI gets `400` and "This SSO Elevator deployment requires elevator CLI 5.0.0 or newer". |
| Slack messages are redesigned, and the request now lives in the buttons. Approve or Discard on a request posted before the upgrade replies "This request was made before an Elevator upgrade — please request again" and removes the buttons. Access granted before the upgrade is still revoked on schedule, but its message is not updated; the revoker posts a standalone line instead. | Anyone with pending requests at upgrade time. | Approve or discard pending requests before the apply, or ask requesters to request again. |
| A Slack-route POST without the `X-Slack-Signature` or `X-Slack-Request-Timestamp` header gets `400` from API Gateway, and the Lambda does not run. Before, the Lambda answered `401`. | Anyone with uptime checks or alerts on that response. | Update them. |
| Audit entries: new `operation_type` values `declined` and `incomplete`, new fields `decision_reason` and `error_message`, and `version` is now `2` (null on older records). | Anyone who queries the audit bucket. | Run the `ALTER TABLE` in [docs/audit.md](docs/audit.md) on an existing Athena table. Make sure consumers accept the new operation types. |
| SnapStart is on for the requester by default (`snap_start = true`). The apply fails where Lambda does not offer SnapStart for Python 3.14 in the region and package type. | Deployments in such a region. | Set `snap_start = false` there. |

**From a release before 4.4.3**, these 4.4.x changes also apply:

- From before 4.4.0: if two Identity Store users share an email, case-insensitively, every request from either of them fails with an "email collision" error. Before, it resolved to whichever came first. Fix the duplicate in Identity Store.
- From before 4.4.0: `max_permissions_duration_time` must be greater than 0. Terraform rejects other values at plan.
- From before 4.4.2: every `permission_duration_list_override` entry must look like `"H:MM"`, for example `"01:30"`. A malformed entry stops the access-requester from starting, which breaks Slack.

## Before the apply

Nothing in this section touches the running deployment.

1. **SnapStart.** `snap_start` is on by default. Lambda does not offer SnapStart for every region and package type, and the apply fails where it is missing: check [Lambda SnapStart](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html) for Python 3.14 in your region, for container images (`use_pre_created_image = true`, the default) or zip. Where it is not offered, set `snap_start = false`.
2. **Image.** If you leave `ecr_repo_tag` unset, the module uses the image built for its own release. If you pin `ecr_repo_tag` or host images yourself (`ecr_repo_name`, `ecr_owner_account_id`), point it at a 5.x image. With `use_pre_created_image = false` the module builds from source and needs nothing here.
3. **Organization.** Unless you set `enable_access_requester_cli = false`, check that the deployment account is in an AWS Organization and that the principal running Terraform has the Organizations read permissions in [docs/cli.md](docs/cli.md).
4. **Old logs.** If you need the HTTP API's access logs, export them now. The apply deletes the log group; the plan shows it as destroyed under `module.http_api`.
5. **SSM parameters.** Check whether parameters already exist at the default names:

   ```sh
   aws ssm describe-parameters \
     --parameter-filters 'Key=Name,Values=/sso-elevator/slack-bot-token,/sso-elevator/slack-signing-secret'
   ```

   If they exist and only this deployment reads them, you delete and recreate them in step 8. If anything else owns or reads them, keep them and set new names instead, for example `slack_bot_token_ssm_parameter_name = "/sso-elevator/v5/slack-bot-token"`. That applies when another Terraform stack manages them as resources, another deployment or tool reads them, or a KMS key policy names them. Two deployments in one account and region always need different names.

   If the output shows a customer managed key in `KeyId`, add `--key-id <key-arn>` to the `put-parameter` commands in step 10. Its key policy must let IAM policies in the account grant access, as the default key policy does.
6. **WAF** (optional). To put AWS WAF in front of the API, set `waf_enabled = true` or `waf_web_acl_arn` ([docs/api-gateway.md](docs/api-gateway.md)). If AWS Firewall Manager attaches a web ACL to your APIs, leave both unset.

## The upgrade

The commands assume the module block is named `aws_sso_elevator` and the default parameter names. Substitute yours. Run them with credentials for the deployment account and region.

7. **Edit the module block, all of it, before any apply.** Even a targeted apply validates the whole configuration, so a removed input left behind fails it with "Unsupported argument".
   - In the root module, require Terraform `>= 1.11` and hashicorp/aws `>= 6.28`.
   - Bump the module `version` to 5.0.0.
   - Remove `slack_bot_token` and `slack_signing_secret`, and whatever fed them: `aws_ssm_parameter` data sources, tfvars entries, `-var` flags, `TF_VAR_*` variables in CI.
   - Remove `create_api_gateway`, `create_lambda_url` and `cli_sso_role_name_prefix`. Rename `event_brige_*` to `event_bridge_*`.
   - Add the settings you chose above: `snap_start`, `ecr_repo_tag`, `enable_access_requester_cli`, `slack_*_ssm_parameter_name`, WAF, `api_gateway_access_logs_enabled`.
   - Run `terraform init -upgrade`.

8. **Move the old parameters out of the way.** Skip this if none exist at the module's names. Save both values into shell variables, without printing them, then delete the parameters. Keep this shell open until step 10.

   ```sh
   SLACK_BOT_TOKEN=$(aws ssm get-parameter --with-decryption --name /sso-elevator/slack-bot-token --query Parameter.Value --output text)
   SLACK_SIGNING_SECRET=$(aws ssm get-parameter --with-decryption --name /sso-elevator/slack-signing-secret --query Parameter.Value --output text)
   aws ssm delete-parameter --name /sso-elevator/slack-bot-token
   aws ssm delete-parameter --name /sso-elevator/slack-signing-secret
   ```

   You can also copy both secrets from the Slack app settings in step 10 instead.

   Don't `terraform import` the existing parameters. With no write-only value to go on, the import stores the decrypted secret in state, and the next apply writes `REPLACE_ME` over the real value. That apply clears the secret from the current state, but the earlier state version still holds it. Both behaviours were confirmed against hashicorp/aws 6.67.0.

9. **Create only the two parameters.** Slack keeps working: the 4.x Lambdas still read the secrets from their environment.

   ```sh
   terraform apply \
     -target='module.aws_sso_elevator.aws_ssm_parameter.slack_bot_token' \
     -target='module.aws_sso_elevator.aws_ssm_parameter.slack_signing_secret'
   ```

   Both parameters now hold the placeholder `REPLACE_ME`.

10. **Write both secrets.** If you didn't save them in step 8, read them in from the Slack app settings without echoing them:

    ```sh
    read -rs SLACK_BOT_TOKEN        # paste the Bot User OAuth Token, then Enter
    read -rs SLACK_SIGNING_SECRET   # paste the Signing Secret, then Enter
    ```

    Then write them. `printf` is a shell builtin, so the secret reaches the AWS CLI through a pipe, not its argument list. `--value "$SLACK_BOT_TOKEN"` would put it in the process list.

    ```sh
    printf '%s' "$SLACK_BOT_TOKEN" | aws ssm put-parameter --overwrite --type SecureString \
      --name /sso-elevator/slack-bot-token --value file:///dev/stdin
    printf '%s' "$SLACK_SIGNING_SECRET" | aws ssm put-parameter --overwrite --type SecureString \
      --name /sso-elevator/slack-signing-secret --value file:///dev/stdin
    unset SLACK_BOT_TOKEN SLACK_SIGNING_SECRET
    ```

11. **Run the full `terraform apply`.** Downtime starts here. The apply switches the Lambdas to the 5.x image and to SSM, replaces the API, and publishes a SnapStart version of the requester; it takes a few extra minutes while Lambda takes the snapshot. The plan destroys the HTTP API, its access-log group and the Lambda Function URL, where they exist.

## After the apply

12. **Slack.** In the Slack app settings, set the Interactivity Request URL (`request_url` in the manifest) to the `requester_api_endpoint_url` output.
13. **CLI.** Every CLI user upgrades `elevator` to 5.0.0 or newer ([install](cmd/elevator/README.md)), then points it at the new route:

    ```sh
    elevator configure --endpoint <requester_api_endpoint_url_cli output>
    ```

    Behind a custom domain, pass the custom URL and add `--api-id` with the `requester_api_id` output. `configure --endpoint` clears a saved API id, so give both in one command. Update `ELEVATOR_ENDPOINT`, and `ELEVATOR_API_ID` for a custom domain, wherever scripts or CI set them: the environment overrides the saved config.
14. **Cross-account callers.** Their permission sets need `execute-api:Invoke` on the `requester_api_execution_arn_cli` output. Replace the old ARN in any policy that still names it.
15. **Verify.** Submit a Slack request and a CLI request, and check both are granted (or posted for approval). Fix anything that fails before going on.
16. **Delete the requester's 4.x versions.** This step is required. Each published version keeps its own code and configuration and stays invocable as `function:<name>:<N>` by anyone allowed `lambda:InvokeFunction` on it. A 4.x version with the CLI route on trusts the caller identity in the event, which a direct invoke can forge, and every 4.x version holds the Slack secrets in its environment.

    The revoker's nightly version pruning does not cover this. It keeps the version `live` points to and the newest Active version below it, and right after the upgrade that is the last 4.x version. Delete every version older than `live`:

    ```sh
    FN=access-requester   # your requester_lambda_name
    LIVE=$(aws lambda get-alias --function-name "$FN" --name live --query FunctionVersion --output text)
    for v in $(aws lambda list-versions-by-function --function-name "$FN" --query 'Versions[].Version' --output text); do
      if [ "$v" != '$LATEST' ] && [ "$v" -lt "$LIVE" ]; then
        aws lambda delete-function --function-name "$FN" --qualifier "$v"
      fi
    done
    ```

    The revoker and attribute-syncer also published 4.x versions holding the bot token in their environment. The next step makes that token worthless; delete those versions too if you prefer.
17. **Rotate both Slack secrets.** The 4.x Terraform state holds them in the Lambda environment variables and in any `aws_ssm_parameter` data source values, and so do the 4.x Lambda versions. Generate a new bot token and signing secret in the Slack app settings and write them as in step 10. A new signing secret needs new access-requester containers right away; follow "Rotating a secret" in [docs/slack.md](docs/slack.md). Then purge the state versions from before the upgrade in your backend, for example the noncurrent versions of the state object in a versioned S3 bucket.
18. **Monitoring.** Update uptime checks and alerts that expect `401` from an unsigned POST to the Slack URL: it now gets `400` from API Gateway.
