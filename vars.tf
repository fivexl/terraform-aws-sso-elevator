variable "tags" {
  description = "A map of tags to assign to resources."
  type        = map(string)
  default     = {}
}

variable "aws_sns_topic_subscription_email" {
  description = "value for the email address to subscribe to the SNS topic"
  type        = string
}

variable "slack_signing_secret" {
  description = "value for the Slack signing secret"
  type        = string
}

variable "slack_bot_token" {
  description = "value for the Slack bot token"
  type        = string
}

variable "log_level" {
  description = "value for the log level"
  type        = string
  default     = "INFO"
}

variable "slack_channel_id" {
  description = "value for the Slack channel ID"
  type        = string
}

variable "schedule_expression" {
  description = "value for the schedule expression"
  type        = string
  default     = "cron(0 23 * * ? *)"
}

variable "schedule_expression_for_check_on_inconsistency" {
  description = "value for the schedule expression for checking on inconsistency"
  type        = string
  default     = "rate(2 hours)"
}

variable "sso_instance_arn" {
  description = "value for the SSO instance ARN"
  type        = string
  default     = ""
}

variable "config" {
  description = "value for the SSO Elevator config"
  type        = any
}

variable "revoker_lambda_name" {
  description = "value for the revoker lambda name"
  type        = string
  default     = "access-revoker"
}

variable "requester_lambda_name" {
  description = "value for the requester lambda name"
  type        = string
  default     = "access-requester"
}

variable "revoker_lambda_name_postfix" {
  type    = string
  default = ""
}

variable "requester_lambda_name_postfix" {
  type    = string
  default = ""
}

variable "schedule_group_name_postfix" {
  type    = string
  default = ""
}

variable "schedule_role_name_postfix" {
  type    = string
  default = ""
}

variable "revoker_post_update_to_slack" {
  description = "Should revoker send a confirmation of the revocation to Slack?"
  type        = bool
  default     = true
}

variable "build_in_docker" {
  description = "Whether to build the lambda in a docker container or using local python (poetry)"
  type        = bool
  default     = true
}
