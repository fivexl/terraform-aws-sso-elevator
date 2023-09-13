resource "aws_sns_topic" "dlq" {
  count             = var.aws_sns_topic_subscription_email != "" ? 1 : 0
  name              = var.requester_lambda_name
  kms_master_key_id = "alias/aws/sns" # tfsec:ignore:aws-sns-topic-encryption-use-cmk
  tags              = var.tags
}

resource "aws_sns_topic_subscription" "dlq" {
  count     = var.aws_sns_topic_subscription_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.dlq[0].arn
  protocol  = "email"
  endpoint  = var.aws_sns_topic_subscription_email
}
