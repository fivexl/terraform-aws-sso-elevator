aws_sns_topic_subscription_email = "email@example.com"
slack_channel_id                 = "slack_channel_id"
sso_instance_arn                 = "sso_instance_arn"
config = [{
  "ResourceType" : "Account",
  "Resource" : "account_id",
  "PermissionSet" : "*",
  "Approvers" : "email@gmail.com",
  "AllowSelfApproval" : true,
}]
