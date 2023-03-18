variable "tags" {
  description = "A map of tags to assign to resources."
  type        = map(string)
  default     = {}
}

variable "aws_sns_topic_subscription_email" {}

variable "slack_signing_secret" {
  type = string
}

variable "slack_bot_token" {
  type = string
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "slack_channel_id" {
  type = string
}

variable "schedule_expression" {
  type    = string
  default = "cron(0 23 * * ? *)"
}

variable "config" {
  type = any
}

variable "identity_provider_arn" {
  type        = string
  description = "ARN of the identity provider. IAM > Identity providers > {Name} > Summary > ARN"
}
