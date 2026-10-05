# Slack

Users request and approve access through a Slack app. Setting it up takes a Terraform apply, the app itself, and two secrets written to SSM Parameter Store. Upgrading from 4.x, where the secrets were module inputs: follow [UPGRADE-5.0.md](../UPGRADE-5.0.md) instead.

## Fresh install

1. Run a full `terraform apply`. The Lambdas start with placeholder secrets: the access-requester refuses to start, and the revoker and attribute-syncer run without Slack until step 3.
2. [Create the Slack app](#create-the-slack-app) with the `requester_api_endpoint_url` output as its request URL.
3. [Write both secrets](#slack-secrets-in-ssm-parameter-store) to SSM.
4. Invite the app to the channel in `slack_channel_id` (`/invite @AWS SSO Access Elevator`). It can only post in channels it is a member of.

No redeploy is needed. A failed access-requester start is not cached, so the next Slack request starts it again and it reads the new values; the revoker and attribute-syncer read the bot token on every invocation.

## Create the Slack app

1. Go to https://api.slack.com/apps and click **Create New App**.
2. Choose **From a manifest**, select the workspace, and choose YAML.
3. Paste the manifest below, with `request_url` set to the `requester_api_endpoint_url` Terraform output.
4. Review the scopes and click **Create**, then **Install to Workspace**.
5. From **Basic Information**, copy the **Signing Secret**. From **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-...`). Write both to SSM as below.

```yaml
display_information:
  name: AWS SSO Access Elevator
  description: Slack bot to temporarily assign AWS SSO permission sets to a user
features:
  bot_user:
    display_name: AWS SSO Access Elevator
    always_online: false
  shortcuts:
    - name: access
      type: global
      callback_id: request_for_access
      description: Request access to Permission Set in AWS Account
    # Delete this shortcut to turn off group requests (group_config)
    - name: group-access
      type: global
      callback_id: request_for_group_membership
      description: Request access to SSO Group
oauth_config:
  scopes:
    bot:
      # Shortcuts
      - commands
      # Post and update request messages
      - chat:write
      # Look up users by email, to match them to IAM Identity Center and mention approvers
      - users:read
      - users:read.email
      # Read earlier request messages in the channel (public / private channel)
      - channels:history
      - groups:history
      # Direct messages to requesters outside the channel (send_dm_if_user_not_in_channel):
      # check channel membership (public / private channel) and send the DM
      - channels:read
      - groups:read
      - im:write
settings:
  interactivity:
    is_enabled: true
    request_url: <requester_api_endpoint_url Terraform output>
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

All Slack traffic (shortcuts, form submissions, button clicks) arrives on the interactivity request URL. The app needs no event subscriptions or slash commands. API Gateway rejects a POST without Slack's signature headers with `400` before it reaches the Lambda; the Lambda then verifies the signature with the signing secret.

## Slack secrets in SSM Parameter Store

The Lambdas read the bot token and signing secret from two SSM SecureString parameters at runtime. The module creates both with the placeholder `REPLACE_ME` through the write-only `value_wo` argument, and the Lambdas treat the placeholder as an unset secret. You write the real values with the AWS CLI.

**Why by hand.** Terraform writes every value it manages into its state in plain text, including a SecureString parameter's `value`. Anyone who can read the state, or an old version of it in a versioned S3 backend, can read the secret. A write-only argument is the one kind of value Terraform never stores, so the module writes only the placeholder and the real secrets never pass through Terraform.

Later applies leave your value alone, with three exceptions that replace the parameter with a fresh placeholder or delete it: changing `slack_*_ssm_parameter_name`, moving the module to a new address without a `moved` block, and `terraform destroy`.

| Variable | Default | Read by |
| -------- | ------- | ------- |
| `slack_bot_token_ssm_parameter_name` | `/sso-elevator/slack-bot-token` | access-requester, revoker, attribute-syncer |
| `slack_signing_secret_ssm_parameter_name` | `/sso-elevator/slack-signing-secret` | access-requester |

Write each secret like this, substituting your parameter names if you changed them. At each `read`, paste the value (first the Bot User OAuth Token, then the Signing Secret) and press Enter:

```sh
read -rs SLACK_BOT_TOKEN
printf '%s' "$SLACK_BOT_TOKEN" | aws ssm put-parameter --overwrite --type SecureString \
  --name /sso-elevator/slack-bot-token --value file:///dev/stdin
read -rs SLACK_SIGNING_SECRET
printf '%s' "$SLACK_SIGNING_SECRET" | aws ssm put-parameter --overwrite --type SecureString \
  --name /sso-elevator/slack-signing-secret --value file:///dev/stdin
unset SLACK_BOT_TOKEN SLACK_SIGNING_SECRET
```

`read -rs` keeps the secret off the screen and out of shell history. `printf` is a shell builtin, so the secret reaches the AWS CLI on stdin and never appears in any process's arguments. Do not use `--value "$SLACK_BOT_TOKEN"`: the expanded value is in the AWS CLI's command line, visible to other users of the machine in the process list. Piping JSON to `--cli-input-json file:///dev/stdin` does not work in AWS CLI v2, which reads the file twice and gets nothing the second time.

Without `--key-id`, SSM encrypts with the AWS managed key `alias/aws/ssm`. To use a customer managed KMS key, add `--key-id <key-arn>`; its key policy must let IAM policies in the account grant access, as the default key policy does. The Lambda roles allow `kms:Decrypt` only through SSM and only for these parameters.

### Rotating a secret

Write the new value with the same commands. The revoker and attribute-syncer pick up a new bot token on their next invocation.

The access-requester reads both secrets at cold start or [SnapStart](api-gateway.md#snapstart) restore, so warm containers keep the old values until they are recycled. A new bot token can wait for that. A new signing secret takes effect in Slack at once, and warm containers reject every Slack request until they restart, so force new containers right after writing it. API Gateway invokes the `live` alias, which points at a published version, so changing `$LATEST` alone is not enough: change the configuration, publish a version and move `live` to it. Set `FN` to your `requester_lambda_name`. With SnapStart, the `wait function-active-v2` step waits until the snapshot is ready.

```sh
FN=access-requester
aws lambda update-function-configuration --function-name "$FN" --description "Slack secret rotated $(date +%s)"
aws lambda wait function-updated --function-name "$FN"
VERSION=$(aws lambda publish-version --function-name "$FN" --query Version --output text)
aws lambda wait function-active-v2 --function-name "$FN" --qualifier "$VERSION"
aws lambda update-alias --function-name "$FN" --name live --function-version "$VERSION"
```

Without the configuration change, `publish-version` returns the existing latest version instead of a new one. The next `terraform apply` sets the description back, which publishes another version and moves `live` to it.

### When a secret is missing

If a secret cannot be read or still holds the placeholder, the access-requester fails at start and every Slack and CLI request errors. The revoker and attribute-syncer log the error and still revoke and sync, without Slack messages. Some revoker invocations then fail after their work is done: a scheduled revocation of a single assignment fails after revoking it, and the Slack-only events (inconsistency reports, removing buttons from expired requests, approver reminders) fail outright. These show up as Lambda errors and, if `aws_sns_topic_subscription_email` is set, as DLQ alerts.
