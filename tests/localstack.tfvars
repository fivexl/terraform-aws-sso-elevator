aws_sns_topic_subscription_email = "email@example.com"
slack_signing_secret             = "slack_signing_secret"
slack_bot_token                  = "slack_bot_token"
slack_channel_id                 = "slack_channel_id"
sso_instance_arn                 = "sso_instance_arn"
config = [{
  "ResourceType" : "Account",
  "Resource" : "account_id",
  "PermissionSet" : "*",
  "Approvers" : "email@gmail.com",
  "AllowSelfApproval" : true,
}]
