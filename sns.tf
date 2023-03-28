resource "aws_sns_topic" "dlq" {
  name = local.requester_lambda_name
  tags = var.tags
}

resource "aws_sns_topic_subscription" "dlq" {
  topic_arn = aws_sns_topic.dlq.arn
  protocol  = "email"
  endpoint  = var.aws_sns_topic_subscription_email
}
