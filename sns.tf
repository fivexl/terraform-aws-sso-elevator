resource "aws_sns_topic" "dlq" {
  name              = local.requester_lambda_name
  kms_master_key_id = "alias/aws/sns"
  tags              = var.tags
}

resource "aws_sns_topic_subscription" "dlq" {
  topic_arn = aws_sns_topic.dlq.arn
  protocol  = "email"
  endpoint  = var.aws_sns_topic_subscription_email
}
