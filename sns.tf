resource "aws_sns_topic" "dlq" {
  name = local.name
  tags = var.tags
}

resource "aws_sns_topic_subscription" "dlq" {
  topic_arn = aws_sns_topic.dlq.arn
  protocol  = "email"
  endpoint  = var.email
}