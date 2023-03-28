# terraform-aws-sso-slack-bot
Slack bot to temporary assign AWS SSO Permission set to a user

## Usage
```terraform

module "aws_sso_elevator" {
  source                           = "./aws-sso-elevator"
  aws_sns_topic_subscription_email = "mobessona2@gmail.com"

  slack_signing_secret = data.aws_ssm_parameter.sso_elevator_slack_signing_secret.value
  slack_bot_token      = data.aws_ssm_parameter.sso_elevator_slack_bot_token.value
  slack_channel_id     = "C04V34WDEQZ"
  schedule_expression  = "cron(0 23 * * ? *)" # revoke access shedule expression

  identity_provider_arn = "arn:aws:iam::************:saml-provider/*************************************"

  config = {
    "users" : [
      {
        "email" : "email",
        "slack_id" : "***********",
        "can_approve" : true
      },
    ],
    "permission_sets" : [
      {
        "name" : "ReadOnly"
      },
      {
        "name" : "AdministratorAccess"
      },
    ],
    "accounts" : [
      {
        "name" : "account-name",
        "id" : "************",
        "approvers" : ["email"]
      },
      {
        "name" : "account-name",
        "id" : "************",
        "approvers" : ["email"]
      }
    ]
  }
}

data "aws_ssm_parameter" "sso_elevator_slack_signing_secret" {
  name = "/sso-elevator/slack-signing-secret"
}

data "aws_ssm_parameter" "sso_elevator_slack_bot_token" {
  name = "/sso-elevator/slack-bot-token"
}

output "aws_sso_elevator_lambda_function_url" {
  value = module.aws_sso_elevator.lambda_function_url
}

```

### Slack App creation
1. Go to https://api.slack.com/
2. Click `create an app`
3. Click `From an app manifest`
4. Select workspace, click `next`
5. Choose `yaml` for app manifest format
6. Update lambda url (from output `aws_sso_elevator_lambda_function_url`) to `request_url` field and paste the following into the text box: 
```yaml
display_information:
  name: AWS SSO access elevator
features:
  bot_user:
    display_name: AWS SSO access elevator
    always_online: false
  shortcuts:
    - name: access
      type: global
      callback_id: acesss23
      description: Elevate AWS SSO access
oauth_config:
  scopes:
    bot:
      - commands
      - chat:write
settings:
  interactivity:
    is_enabled: true
    request_url: <LAMBDA URL GOES HERE>
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```
7. Check permissions and click `create`
8. Click `install to workspace`
9. Copy `Signing Secret` # for `slack_signing_secret` module input
10. Copy `Bot User OAuth Token` # for `slack_bot_token` module input