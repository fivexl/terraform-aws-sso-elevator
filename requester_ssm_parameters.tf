# The access-requester Lambda reads its configuration from SSM Parameter Store instead of
# Lambda environment variables. Environment variables are stored and displayed in plaintext
# on the function configuration, so the Slack bot token and signing secret were readable by
# anyone with lambda:GetFunctionConfiguration. Parameter Store keeps them as SecureString,
# encrypted at rest with KMS and readable only through an explicit ssm:GetParametersByPath grant.

locals {
  requester_ssm_parameter_path = trimsuffix(var.requester_ssm_parameter_path, "/")

  # SSM stores strings only, so non-string values are converted explicitly. The keys are the
  # environment variable names the Lambda used before, and config.py maps them back onto the
  # Config fields by lowercasing the last segment of the parameter name.
  requester_ssm_config = {
    SLACK_CHANNEL_ID    = var.slack_channel_id
    SCHEDULE_GROUP_NAME = var.schedule_group_name

    SSO_INSTANCE_ARN                            = local.sso_instance_arn
    SCHEDULE_POLICY_ARN                         = aws_iam_role.eventbridge_role.arn
    REVOKER_FUNCTION_ARN                        = local.revoker_lambda_arn
    REVOKER_FUNCTION_NAME                       = var.revoker_lambda_name
    S3_BUCKET_FOR_AUDIT_ENTRY_NAME              = local.s3_bucket_name
    S3_BUCKET_PREFIX_FOR_PARTITIONS             = var.s3_bucket_partition_prefix
    SSO_ELEVATOR_SCHEDULED_REVOCATION_RULE_NAME = aws_cloudwatch_event_rule.sso_elevator_scheduled_revocation.name
    REQUEST_EXPIRATION_HOURS                    = tostring(var.request_expiration_hours)
    APPROVER_RENOTIFICATION_INITIAL_WAIT_TIME   = tostring(var.approver_renotification_initial_wait_time)
    APPROVER_RENOTIFICATION_BACKOFF_MULTIPLIER  = tostring(var.approver_renotification_backoff_multiplier)
    MAX_PERMISSIONS_DURATION_TIME               = tostring(var.max_permissions_duration_time)
    PERMISSION_DURATION_LIST_OVERRIDE           = jsonencode(var.permission_duration_list_override)
    SECONDARY_FALLBACK_EMAIL_DOMAINS            = jsonencode(var.secondary_fallback_email_domains)
    SEND_DM_IF_USER_NOT_IN_CHANNEL              = tostring(var.send_dm_if_user_not_in_channel)
    CONFIG_BUCKET_NAME                          = local.config_bucket_name
    CONFIG_S3_KEY                               = "config/approval-config.json"
    CACHE_ENABLED                               = tostring(var.cache_enabled)
  }

  requester_ssm_secrets = {
    SLACK_BOT_TOKEN      = var.slack_bot_token
    SLACK_SIGNING_SECRET = var.slack_signing_secret
  }

  # Any parameter change bumps that parameter's version, which changes this value and therefore
  # publishes a new Lambda version. Without it a configuration change would only take effect on
  # the next natural cold start, unlike the environment variables it replaces.
  requester_ssm_config_version = sha256(jsonencode(merge(
    { for name, parameter in aws_ssm_parameter.requester_config : name => parameter.version },
    { for name, parameter in aws_ssm_parameter.requester_secret : name => parameter.version },
  )))
}

resource "aws_ssm_parameter" "requester_config" {
  for_each = local.requester_ssm_config

  name        = "${local.requester_ssm_parameter_path}/${each.key}"
  description = "SSO Elevator access-requester configuration: ${each.key}"
  type        = "String"
  value       = each.value
  tags        = var.tags
}

resource "aws_ssm_parameter" "requester_secret" {
  for_each = local.requester_ssm_secrets

  name        = "${local.requester_ssm_parameter_path}/${each.key}"
  description = "SSO Elevator access-requester secret: ${each.key}"
  type        = "SecureString"
  key_id      = var.ssm_parameter_kms_key_id
  value       = each.value
  tags        = var.tags
}
