# terraform-aws-sso-slack-bot
Slack bot to temporary assign AWS SSO Permission set to a user

## Usage
```terraform

module "aws_sso_elevator" {
  source                           = "./aws-sso-elevator"
  
  slack_signing_secret             = "********************************" # you will get it after app creation
  slack_bot_token                  = "xoxb-************-*************-************************" # you will get it after app creation
  slack_channel_id                 = "***********"
  schedule_expression              = "cron(0 23 * * ? *)" # revoke access shedule expression
  identity_provider_arn = "arn:aws:iam::************:saml-provider/*************************************"
  # IAM > Identity provider > MyIdentityProvider > Summary > ARN
 
  config = {
      "users" : [
        {
          "sso_id" : "**********-********-****-****-****-************",
          # IAM Identity Center > Users > MyUserName > General information > User ID
          "email" : "email",
          "slack_id" : "***********",
          "can_approve" : true
        },
        {
          "sso_id" : "**********-********-****-****-****-************",
          "email" : "email",
          "slack_id" : "***********",
          "can_approve" : false
        },
      ],
      "permission_sets" : [
        { "name" : "ReadOnlyPlus",
        "arn" : "arn:aws:sso:::permissionSet/ssoins-****************/ps-****************"},
        # IAM Identity Center > Permission sets > MyPermissionSet > General settings > ARN
      ], 
      "accounts" : [
        {
          "name" : "AWS account name"
          "id" : "************",
          "approvers" : ["email"]
        }
      ]
    }
    
    aws_sns_topic_subscription_email = "email"
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