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
  description = "recovation schedule expression (will revoke all user-level assignments unknown to the Elevator)"
  type        = string
  default     = "cron(0 23 * * ? *)"
}

variable "schedule_expression_for_check_on_inconsistency" {
  description = "how often revoker should check for inconsistency (warn if found unknown user-level assignments)"
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

variable "event_brige_check_on_inconsistency_rule_name" {
  description = "value for the event bridge check on inconsistency rule name"
  type        = string
  default     = "sso_elevator_check_on_inconsistency"
}

variable "event_brige_scheduled_revocation_rule_name" {
  description = "value for the event bridge scheduled revocation rule name"
  type        = string
  default     = "sso_elevator_scheduled_revocation"
}

variable "schedule_group_name" {
  description = "value for the schedule group name"
  type        = string
  default     = "sso-elevator-scheduled-revocation" # TODO fix format to snake case
}

variable "schedule_role_name" {
  description = "value for the schedule role name"
  type        = string
  default     = "event-bridge-role-for-sso-elevator" # TODO fix format to snake case
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

variable "s3_bucket_name_for_audit_entry" {
  description = "Unique name of the S3 bucket"
  type        = string
  default     = "sso-elevator-audit-entry"
}

variable "s3_bucket_partition_prefix" {
  description = "The prefix for the S3 bucket partitions"
  type        = string
  default     = "logs"
}

variable "s3_name_of_the_existing_bucket" {
  description = "Specify the name of an existing S3 bucket to use. If not provided, a new bucket will be created."
  type        = string
  default     = ""
}

variable "s3_mfa_delete" {
  description = "Whether to enable MFA delete for the S3 bucket"
  type        = bool
  default     = false
}

variable "s3_object_lock" {
  description = "Enable object lock"
  type        = bool
  default     = false
}

variable "s3_object_lock_configuration" {
  description = "Object lock configuration"
  type        = any
  default = { rule = {
    default_retention = {
      mode  = "GOVERNANCE"
      years = 2
    }
  } }
}

variable "s3_logging" {
  description = "Map containing access bucket logging configuration."
  type        = map(string)
  default     = {}
}

variable "request_expiration_hours" {
  description = "After how many hours should the request expire? If set to 0, the request will never expire."
  type        = number
  default     = 8
}

variable "approver_renotification_initial_wait_time" {
  description = "The initial wait time before the first re-notification to the approver is sent. This is measured in minutes. If set to 0, no re-notifications will be sent."
  type        = number
  default     = 15
}

variable "approver_renotification_backoff_multiplier" {
  description = "The multiplier applied to the wait time for each subsequent notification sent to the approver. Default is 2, which means the wait time will double for each attempt."
  type        = number
  default     = 2
}
