variable "create_api_gateway" {
  description = "If true, module will create & configure API Gateway for the Lambda function"
  type        = bool
  default     = true
}

variable "ecr_repo_name" {
  description = "The name of the ECR repository."
  type        = string
  default     = "aws-sso-elevator"
}

variable "ecr_repo_tag" {
  description = "The tag of the image in the ECR repository."
  type        = string
  default     = "4.1.0"
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
  description = "DEPRECATED: Use event_bridge_check_on_inconsistency_rule_name instead. This variable contains a typo and will be removed in a future version."
  type        = string
  default     = "sso-elevator-check-on-inconsistency"
}

variable "event_brige_scheduled_revocation_rule_name" {
  description = "DEPRECATED: Use event_bridge_scheduled_revocation_rule_name instead. This variable contains a typo and will be removed in a future version."
  type        = string
  default     = "sso-elevator-scheduled-revocation"
}

variable "event_bridge_check_on_inconsistency_rule_name" {
  description = "value for the event bridge check on inconsistency rule name"
  type        = string
  default     = null
}

variable "event_bridge_scheduled_revocation_rule_name" {
  description = "value for the event bridge scheduled revocation rule name"
  type        = string
  default     = null
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
  description = <<EOT
  The name of the S3 bucket that will be used by the module to store logs about every access request.
  If s3_name_of_the_existing_bucket is not provided, the module will create a new bucket with this name.
  EOT
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
  description = <<EOT
  Name of an existing S3 bucket to use for storing SSO Elevator audit logs.
  An audit log bucket is mandatory.
  If you specify this variable, the module will use your existing bucket.
  Otherwise, if you don't provide this variable, the module will create a new bucket named according to the "s3_bucket_name_for_audit_entry" variable.
  If the module is creating an audit bucket for you, then you must provide a logging configuration via the s3_logging input variable, with at least the target_bucket key specified.
  EOT
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
  description = <<EOT
  Map containing access bucket logging configuration.
  If you are not providing s3_name_of_the_existing_bucket variable, then module will create bucket for you.
  If the module is creating an audit bucket for you, then you must provide a logging configuration via this input variable, with at least the target_bucket key specified.
  EOT
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
  description = <<EOT
  Maximum duration (in hours) for permissions granted by Elevator. Max number - 48 hours.
  Due to Slack's dropdown limit of 100 items, anything above 48 hours will cause issues when generating half-hour increments
  and Elevator will not display more then 48 hours in the dropdown.
  EOT
  type        = number
  default     = 24
}

variable "permission_duration_list_override" {
  description = <<EOT
  An explicit list of duration values to appear in the drop-down menu users use to select how long to request permissions for.
  Each entry in the list should be formatted as "hh:mm", e.g. "01:30" for an hour and a half. Note that while the number of minutes
  must be between 0-59, the number of hours can be any number.
  If this variable is set, the max_permission_duration_time is ignored.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for d in var.permission_duration_list_override : can(regex("^\\d+:[0-5]\\d$", d))])
    error_message = "Each entry in the permission_duration_list_override must be in the format hh:mm, that is, a number of hours, followed by a colon, followed by a number of minutes."
  }
}

variable "logs_retention_in_days" {
  description = "The number of days you want to retain log events in the log group for both Lambda functions and API Gateway."
  type        = number
  default     = 365
}

variable "secondary_fallback_email_domains" {
  type        = list(string)
  default     = []
  description = <<EOT

Value example: ["@new.domain", "@second.domain"], every domain name should start with "@".
WARNING: 
This feature is STRONGLY DISCOURAGED because it can introduce security risks and open up potential avenues for abuse.

SSO Elevator uses Slack email addresses to find users in AWS SSO. In some cases, the domain of a Slack user's email 
(e.g., "john.doe@old.domain") differs from the domain defined in AWS SSO (e.g., "john.doe@new.domain"). By setting 
these fallback domains, SSO Elevator will attempt to replace the original domain from Slack with each secondary domain 
in order to locate a matching AWS SSO user. 
 
Use Cases:
- This mechanism should only be used in rare or critical situations where you cannot align Slack and AWS SSO domains.

Use Case Example:
- Slack email: john.doe@old.domain
- AWS SSO email: john.doe@new.domain

Without fallback domains, SSO Elevator cannot find the SSO user due to the domain mismatch. By setting 
secondary_fallback_email_domains = ["@new.domain"], SSO Elevator will swap out "@old.domain" for "@new.domain"
(and any other domain in the list) and attempt to locate "john.doe@new.domain" in AWS SSO.

Security Risks & Recommendations:
- If multiple SSO users share the same local-part (before the "@") across different domains, SSO Elevator may 
  grant permissions to the wrong user.
- Disable or remove entries in this variable as soon as you no longer need domain fallback functionality 
  to restore a more secure configuration.

IN SUMMARY:
Use "secondary_fallback_email_domains" ONLY if absolutely necessary. It is best practice to maintain 
consistent, verified email domains in Slack and AWS SSO. Remove these fallback entries as soon as you 
resolve the underlying domain mismatch to minimize security exposure.

Notes:
- SSO Elevator always prioritizes the primary domain from Slack (the Slack user's email) when searching for a user in AWS SSO.
- SSO Elevator adds a large warning message in Slack if it uses a secondary fallback domain to find a user in AWS SSO.
- The secondary domain feature works **ONLY** for the requester, approvers in the configuration must have the same email domain as in Slack.
EOT
}

variable "api_gateway_throttling_burst_limit" {
  description = "The maximum number of requests that API Gateway allows in a burst."
  type        = number
  default     = 5
}

variable "api_gateway_throttling_rate_limit" {
  description = "The maximum number of requests that API Gateway allows per second."
  type        = number
  default     = 1
}

variable "api_gateway_name" {
  description = "The name of the API Gateway for SSO Elevator's access-requester Lambda"
  type        = string
  default     = "sso-elevator-access-requster"
}

variable "send_dm_if_user_not_in_channel" {
  type        = bool
  default     = true
  description = <<EOT
If the user is not in the SSO Elevator channel, Elevator will send them a direct message with the request status 
(waiting for approval, declined, approved, etc.) and the result of the request.
Using this feature requires the following Slack app permissions: "channels:read", "groups:read", and "im:write". 
Please ensure these permissions are enabled in the Slack app configuration.
EOT
}

variable "lambda_timeout" {
  description = "The amount of time your Lambda Function has to run in seconds."
  type        = number
  default     = 30
}

variable "lambda_architecture" {
  description = "The instruction set architecture for Lambda functions. Valid values are 'x86_64' or 'arm64'. Use 'arm64' for better price/performance on Graviton2."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.lambda_architecture)
    error_message = "lambda_architecture must be either 'x86_64' or 'arm64'"
  }
}

variable "lambda_memory_size" {
  description = "Amount of memory in MB your Lambda Function can use at runtime. Valid value between 128 MB to 10,240 MB (10 GB), in 64 MB increments."
  type        = number
  default     = 256
}

variable "config_bucket_name" {
  description = "Name of the S3 bucket for storing configuration and cache data (accounts, permission sets, and future config files)"
  type        = string
  default     = "sso-elevator-config"
}

variable "cache_enabled" {
  description = "Enable caching of AWS accounts and permission sets in S3. If set to false, caching is disabled but the S3 bucket will still be created for future config storage."
  type        = bool
  default     = true
}

variable "config_bucket_kms_key_arn" {
  description = "ARN of the KMS key to use for config S3 bucket encryption. If not provided, uses AES256 encryption."
  type        = string
  default     = null
}


# ==========================================
# Attribute Sync Variables
# ==========================================

variable "attribute_sync_enabled" {
  description = "Enable attribute-based group sync feature. When enabled, users will be automatically added to groups based on their Identity Store attributes."
  type        = bool
  default     = false
}

variable "attribute_sync_managed_groups" {
  description = "List of group names to manage via attribute sync. Only these groups will be monitored and modified by the sync process."
  type        = list(string)
  default     = []
}

variable "attribute_sync_rules" {
  description = <<EOT
Attribute mapping rules for group sync. Each rule specifies a group name and the attribute conditions that must be met for a user to be added to that group.
Example:
[
  {
    group_name = "Engineering"
    attributes = {
      department = "Engineering"
      employeeType = "FullTime"
    }
  }
]
EOT
  type = list(object({
    group_name = string
    attributes = map(string)
  }))
  default = []
}

variable "attribute_sync_manual_assignment_policy" {
  description = "Policy for handling manual assignments (users in managed groups who don't match any rules): 'warn' only logs and notifies, 'remove' automatically removes them."
  type        = string
  default     = "remove"

  validation {
    condition     = contains(["warn", "remove"], var.attribute_sync_manual_assignment_policy)
    error_message = "attribute_sync_manual_assignment_policy must be either 'warn' or 'remove'"
  }
}

variable "attribute_sync_schedule" {
  description = "Schedule expression for attribute sync (e.g., 'rate(1 hour)' or 'cron(0 * * * ? *)'). Determines how often the sync runs."
  type        = string
  default     = "rate(1 hour)"
}

variable "attribute_sync_lambda_memory" {
  description = "Memory allocation for attribute syncer Lambda (MB). Increase for large user/group sets."
  type        = number
  default     = 512
}

variable "attribute_sync_lambda_timeout" {
  description = "Timeout for attribute syncer Lambda (seconds). Increase for large user/group sets."
  type        = number
  default     = 300
}

variable "attribute_syncer_lambda_name" {
  description = "Name for the attribute syncer Lambda function."
  type        = string
  default     = "attribute-syncer"
}

variable "attribute_sync_event_rule_name" {
  description = "Name for the EventBridge rule that triggers the attribute syncer."
  type        = string
  default     = "sso-elevator-attribute-sync"
}

variable "identity_store_id" {
  description = "The Identity Store ID. If not provided and sso_instance_arn is also not provided, it will be automatically discovered."
  type        = string
  default     = ""
}

variable "slack_handler_provisioned_concurrent_executions" {
  description = "Provisioned concurrent executions for the Slack handler Lambda. Set to a positive number to reduce cold starts."
  type        = number
  default     = -1
}
