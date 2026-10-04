# Slack

## Slack secrets in SSM Parameter Store

The Lambdas read the Slack bot token and signing secret from two SSM SecureString parameters at runtime; no secret passes through Terraform. The module creates both parameters with the placeholder `REPLACE_ME` through the write-only `value_wo` argument, so neither the placeholder nor the real value is stored in Terraform state. The Lambdas treat the placeholder as an unset secret. Write-only arguments need Terraform >= 1.11 and the module needs hashicorp/aws >= 6.28.

**Why you set the secrets by hand.** Terraform writes every value it manages into its state file in plain text, and that includes a SecureString parameter's `value`. Anyone who can read the state, or an old version of it in a versioned S3 backend, can read the secret. A write-only argument is the one kind of value Terraform never stores. So the module writes only a placeholder through `value_wo`, and you put the real secrets in with the AWS CLI. They never pass through Terraform. The procedures below are ordered to keep it that way.

Later applies leave the value you set alone, with three exceptions: changing `slack_*_ssm_parameter_name` or moving the module to a new address without a `moved` block replaces the parameter with a fresh placeholder, and `terraform destroy` deletes it.

| Variable | Default | Read by |
| -------- | ------- | ------- |
| `slack_bot_token_ssm_parameter_name` | `/sso-elevator/slack-bot-token` | access-requester, revoker, attribute-syncer |
| `slack_signing_secret_ssm_parameter_name` | `/sso-elevator/slack-signing-secret` | access-requester |

The commands below assume the module block is named `aws_sso_elevator`, as in the [deployment example](#terraform-deployment-example), and the default parameter names. Substitute your own module name and, if you set `slack_*_ssm_parameter_name`, your parameter names.

To keep the secrets out of shell history and the process list, read each one into a variable first and pass the variable:

```sh
read -rs SLACK_BOT_TOKEN        # paste the Bot User OAuth Token, then Enter
read -rs SLACK_SIGNING_SECRET   # paste the Signing Secret, then Enter
aws ssm put-parameter --overwrite --type SecureString --name /sso-elevator/slack-bot-token --value "$SLACK_BOT_TOKEN"
aws ssm put-parameter --overwrite --type SecureString --name /sso-elevator/slack-signing-secret --value "$SLACK_SIGNING_SECRET"
unset SLACK_BOT_TOKEN SLACK_SIGNING_SECRET
```

Without `--key-id`, SSM encrypts with the AWS managed key `alias/aws/ssm`. To use a customer managed KMS key, add `--key-id <key-arn>`; its key policy must allow IAM policies in the account to grant access, as the default key policy does. The Lambda roles allow `kms:Decrypt` only through SSM and only for these parameters.

### Fresh install

1. Run a full `terraform apply`. The Lambdas start with the placeholder: the access-requester refuses to start and the others run without Slack until step 3.
2. [Create the Slack app](#slack-app-creation) with the request URL from the Terraform output.
3. Write both secrets with the `put-parameter` commands above.

No redeploy is needed. A failed access-requester start is not cached, so the next Slack request starts it again and it reads the new values; the revoker and attribute-syncer read the bot token on every invocation.

### Rotating a secret

Write the new value with `put-parameter` as above. The revoker and attribute-syncer read the bot token on every invocation. The access-requester reads both secrets at cold start or [SnapStart](#snapstart) restore, so warm containers keep the old values until they are recycled. A new bot token can wait for that, but a new signing secret takes effect in Slack immediately, and warm access-requester containers reject every Slack request until they restart. Force new containers right after writing it. API Gateway invokes the `live` alias, which points at a published version, so changing `$LATEST` alone is not enough: change the configuration, publish a version and move `live` to it.

```sh
FN=access-requester   # your requester_lambda_name
aws lambda update-function-configuration --function-name "$FN" --description "Slack secret rotated $(date +%s)"
aws lambda wait function-updated --function-name "$FN"
VERSION=$(aws lambda publish-version --function-name "$FN" --query Version --output text)
aws lambda wait function-active-v2 --function-name "$FN" --qualifier "$VERSION"   # SnapStart: until the snapshot is ready
aws lambda update-alias --function-name "$FN" --name live --function-version "$VERSION"
```

Without the configuration change, `publish-version` returns the existing latest version instead of a new one. The next `terraform apply` sets the description back, which publishes another version and moves `live` to it.

If a secret cannot be read or still holds the placeholder, the access-requester fails at start and every Slack request errors. The revoker and attribute-syncer log the error and still revoke and sync, without Slack messages. Some revoker invocations then fail after their work is done: a scheduled revocation of a single assignment fails after revoking it, and the Slack-only events (inconsistency reports, removing buttons from expired requests, approver reminders) fail outright. These show up as Lambda errors and, if `aws_sns_topic_subscription_email` is set, as DLQ alerts.

## Slack App creation
1. Go to https://api.slack.com/
2. Click `create an app`
3. Click `From an app manifest`
4. Select workspace, click `next`
5. Choose `yaml` for app manifest format
6. Update the Request URL (from output `requester_api_endpoint_url`) to the `request_url` field and paste the following into the text box: 
```yaml
display_information:
  name: AWS SSO Access Elevator
  description: Slack bot to temporary assign AWS SSO Permission set to a user
features:
  bot_user:
    display_name: AWS SSO Access Elevator
    always_online: false
  shortcuts:
    - name: access
      type: global
      callback_id: request_for_access
      description: Request access to Permission Set in AWS Account
    - name: group-access # Delete this shortcut if you want to prohibit access to the Group Assignments Mode
      type: global
      callback_id: request_for_group_membership
      description: Request access to SSO Group
oauth_config:
  scopes:
    bot:
      # 'commands': This permission adds shortcuts and/or slash commands that people can use.
      - commands
      # 'chat:write': This permission is required for the app to post messages to Slack.
      - chat:write
      # 'users:read' and 'users:read.email': These permissions are required for the app to find the user's email address, which is necessary for  creating AWS account assignments and including user mentions in requests.
      - users:read.email
      - users:read
      # 'channels:history': This permission is needed for the app to find old messages in order to handle "discard button" events.
      - channels:history
      # Permissions below are required if you want to use the feature of sending direct messages to users if they are not in the channel
      - "channels:read", # View basic information about public channels in a workspace. It allows app to determine if requester is in the channel.
      - "groups:read", # View basic information about private channels that slack app has been added to. Same as above but for private channels
      - "im:write" # Allows the app to send direct messages to members of a workspace. It is used to send messages to the user if they are not in the channel.
settings:
  interactivity:
    is_enabled: true
    request_url: <requester_api_endpoint_url Terraform output>
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```
7. Check permissions and click `create`
8. Click `install to workspace`
9. Copy `Signing Secret` and store it in the `/sso-elevator/slack-signing-secret` SSM parameter (see [Slack secrets in SSM Parameter Store](#slack-secrets-in-ssm-parameter-store))
10. Copy `Bot User OAuth Token` and store it in the `/sso-elevator/slack-bot-token` SSM parameter
