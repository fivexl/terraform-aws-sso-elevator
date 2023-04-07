# terraform-aws-sso-slack-bot
Slack bot to temporary assign AWS SSO Permission set to a user

## Usage
```terraform

module "aws_sso_elevator" {
  source                           = "./aws-sso-elevator"
  aws_sns_topic_subscription_email = "email@gmail.com"

  slack_signing_secret = data.aws_ssm_parameter.sso_elevator_slack_signing_secret.value
  slack_bot_token      = data.aws_ssm_parameter.sso_elevator_slack_bot_token.value
  slack_channel_id     = "***********"
  schedule_expression  = "cron(0 23 * * ? *)" # revoke access shedule expression

  sso_instance_arn = "arn:aws:sso:::instance/ssoins-***********"


  #class Statement:
  #  resource_type: list["Account"]
  #  resource: list[Union[str, Literal["*"]]]
  #  permission_set: list[Union[str, Literal["*"]]]
  #  approvers: Optional[list[str]]
  #  approval_is_not_required: bool = False
  #  allow_self_approval: bool = False

  config = [
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : "*",
      "Approvers" : "email@gmail.com",
      "AllowSelfApproval" : true,
    },
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : "Billing",
      "Approvers" : "email@gmail.com",
      "AllowSelfApproval" : true,
    },
    {
      "ResourceType" : "Account",
      "Resource" : ["account_id", "account_id"],
      "PermissionSet" : "ReadOnlyPlus",
      "Approvers" : "email@gmail.com",
    },
    {
      "ResourceType" : "Account",
      "Resource" : "*",
      "PermissionSet" : "ReadOnlyPlus",
      "ApprovalIsNotRequired" : true,
    },
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : ["ReadOnlyPlus", "AdministratorAccess"],
      "Approvers" : ["email@gmail.com"], 
      "AllowSelfApproval" : true,
    },
    {
      # # No rescuer hath the rescuer.
      # # No Lord hath the champion,
      # # no mother and no father,
      # # only nothingness above.
      # "ResourceType" : "Account",
      # "Resource" : "*",
      # "PermissionSet" : "*",
      # "Approvers" : "org_wide_approver@gmail.com",
      # "AllowSelfApproval" : true,
    },
  ]
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
