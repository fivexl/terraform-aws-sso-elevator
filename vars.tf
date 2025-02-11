variable "create_api_gateway" {
  description = "If true, module will create & configure API Gateway for the Lambda function"
  type        = bool
  default     = true
}

variable "create_lambda_url" {
  description = <<-EOT
  If true, the Lambda function will continue to use the Lambda URL, which will be deprecated in the future
  If false, Lambda url will be deleted.
  EOT
  type        = bool
  default     = true
}

variable "ecr_repo_name" {
  description = "The name of the ECR repository."
  type        = string
  default     = "aws-sso-elevator"
}

variable "use_pre_created_image" {
  description = "If true, the image will be pulled from the ECR repository. If false, the image will be built using Docker from the source code."
  type        = bool
  default     = true
}

variable "ecr_owner_account_id" {
  description = "In what account is the ECR repository located."
  type        = string
  default     = "222341826240"
}

variable "tags" {
  description = "A map of tags to assign to resources."
  type        = map(string)
  default     = {}
}

variable "aws_sns_topic_subscription_email" {
  description = "value for the email address to subscribe to the SNS topic"
  type        = string
  default     = ""
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
  default     = []
}

variable "group_config" {
  description = "value for the SSO Elevator group config"
  type        = any
  default     = []
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
  default     = "sso-elevator-check-on-inconsistency"
}

variable "event_brige_scheduled_revocation_rule_name" {
  description = "value for the event bridge scheduled revocation rule name"
  type        = string
  default     = "sso-elevator-scheduled-revocation"
}

variable "schedule_group_name" {
  description = "value for the schedule group name"
  type        = string
  default     = "sso-elevator-scheduled-revocation"
}

variable "schedule_role_name" {
  description = "value for the schedule role name"
  type        = string
  default     = "sso-elevator-event-bridge-role"
}

variable "revoker_post_update_to_slack" {
  description = "Should revoker send a confirmation of the revocation to Slack?"
  type        = bool
  default     = true
}

variable "s3_bucket_name_for_audit_entry" {
  description = "Unique name of the S3 bucket"
  type        = string
  default     = "sso-elevator-audit-entry"
}

variable "s3_bucket_partition_prefix" {
  description = <<EOT
  The prefix for the S3 audit bucket object partitions.
  Don't use slashes (/) in the prefix, as it will be added automatically, e.g. "logs" will be transformed to "logs/".
  If you want to use the root of the bucket, leave this empty.
  EOT
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

variable "max_permissions_duration_time" {
  description = "Maximum duration of the permissions granted by the Elevator in hours."
  type        = number
  default     = 24
}

variable "logs_retention_in_days" {
  description = "The number of days you want to retain log events in the log group for both Lambda functions and API Gateway."
  type        = number
  default     = 365
}

variable "secondary_fallback_email_domains" {
  type    = list(string)
  default = []
  description = <<EOT
WARNING: 
This feature is STRONGLY DISCOURAGED because it can introduce security risks and open up potential avenues for abuse.

SSO Elevator uses Slack email addresses to find users in AWS SSO. In some cases, the domain of a Slack user's email 
(e.g., "john.doe@gmail.com") differs from the domain defined in AWS SSO (e.g., "john.doe@example.com"). By setting 
these fallback domains, SSO Elevator will attempt to replace the original domain from Slack with each secondary domain 
in order to locate a matching AWS SSO user. 
 
Use Cases:
- This mechanism should only be used in rare or critical situations where you cannot align Slack and AWS SSO domains.

Example:
- Slack email: john.doe@gmail.com
- AWS SSO email: john.doe@example.com

Without fallback domains, SSO Elevator cannot find the SSO user due to the domain mismatch. By setting 
secondary_fallback_email_domains = ["example.com"], SSO Elevator will swap out "gmail.com" for "example.com"
(and any other domain in the list) and attempt to locate "john.doe@example.com" in AWS SSO.

Security Risks & Recommendations:
- If multiple SSO users share the same local-part (before the "@") across different domains, SSO Elevator may 
  grant permissions to the wrong user.
- Disable or remove entries in this variable as soon as you no longer need domain fallback functionality 
  to restore a more secure configuration.

IN SUMMARY:
Use "secondary_fallback_email_domains" ONLY if absolutely necessary. It is best practice to maintain 
consistent, verified email domains in Slack and AWS SSO. Remove these fallback entries as soon as you 
resolve the underlying domain mismatch to minimize security exposure.

Note: SSO Elevator will send message in channel if fallback domains are in use.
EOT
}

