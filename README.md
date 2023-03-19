# terraform-aws-sso-slack-bot
Slack bot to temporary assign AWS SSO Permission set to a user



## Slack App manifest

Make sure to paste Lambda URL to `request_url` field

```
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
## Deploying
```terraform

module "aws_sso_elevator" {
  source                           = "./aws-sso-elevator"
  
  slack_signing_secret             = "********************************"
  slack_bot_token                  = "xoxb-************-*************-************************"
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
```

