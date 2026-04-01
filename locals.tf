locals {
  # Full python version is used for checking the python version before deployment in check_python_version.tf
  full_python_version = "3.13.0"
  # Python version is used for building the docker image in slack_handler_lambda.tf/perm_revoker_lambda.tf/layers.tf
  python_version = join(".", slice(split(".", local.full_python_version), 0, 2))

  revoker_lambda_arn   = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.revoker_lambda_name}"
  requester_lambda_arn = var.slack_handler_provisioned_concurrent_executions > 0 ? "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.requester_lambda_name}:live" : "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.requester_lambda_name}"
  sso_instance_arn     = var.sso_instance_arn == "" ? data.aws_ssoadmin_instances.all[0].arns[0] : var.sso_instance_arn
  identity_store_id    = var.sso_instance_arn == "" ? data.aws_ssoadmin_instances.all[0].identity_store_ids[0] : var.identity_store_id

  # In case of default value for var.s3_bucket_name_for_audit_entry, we append a random string to the bucket name to make it unique.
  # In case of non-default value for var.s3_bucket_name_for_audit_entry, we use the value as is and expect the name is unique.
  # In case of var.s3_name_of_the_existing_bucket, we skip creating a new bucket and use the existing one.
  s3_bucket_name_for_audit_entry = var.s3_bucket_name_for_audit_entry != "sso-elevator-audit-entry" ? var.s3_bucket_name_for_audit_entry : "sso-elevator-audit-entry-${random_string.random.result}"
  s3_bucket_name                 = var.s3_name_of_the_existing_bucket != "" ? var.s3_name_of_the_existing_bucket : local.s3_bucket_name_for_audit_entry
  s3_bucket_arn                  = "arn:aws:s3:::${local.s3_bucket_name}"

  # In case of default value for var.config_bucket_name, we append a random string to the bucket name to make it unique.
  # In case of non-default value for var.config_bucket_name, we use the value as is and expect the name is unique.
  config_bucket_name = var.config_bucket_name != "sso-elevator-config" ? var.config_bucket_name : "sso-elevator-config-${random_string.random.result}"

  # HTTP API configuration
  api_resource_path = "/access-requester"
  api_stage_name    = "default"
  full_api_url      = var.create_api_gateway ? "${module.http_api[0].stage_invoke_url}${local.api_resource_path}" : ""

  api_gateway_allowed_triggers = var.create_api_gateway ? {
    AllowExecutionFromAPIGateway = {
      service    = "apigateway"
      source_arn = "${module.http_api[0].api_execution_arn}/*/*${local.api_resource_path}"
    }
  } : {}

  # Event Bridge rule names with fallback to deprecated variables
  event_bridge_check_on_inconsistency_rule_name = coalesce(
    var.event_bridge_check_on_inconsistency_rule_name,
    var.event_brige_check_on_inconsistency_rule_name
  )
  event_bridge_scheduled_revocation_rule_name = coalesce(
    var.event_bridge_scheduled_revocation_rule_name,
    var.event_brige_scheduled_revocation_rule_name
  )

  # Attribute sync configuration
  attribute_sync_event_rule_name = var.attribute_sync_event_rule_name

  # Attribute sync validation errors
  attribute_sync_validation_errors = concat(
    var.attribute_sync_enabled && length(var.attribute_sync_managed_groups) == 0 ?
    ["attribute_sync_managed_groups must not be empty when attribute_sync_enabled is true"] : [],

    var.attribute_sync_enabled && length(var.attribute_sync_rules) == 0 ?
    ["attribute_sync_rules must not be empty when attribute_sync_enabled is true"] : [],

    # Validate identity_store_id is provided when sso_instance_arn is provided
    var.attribute_sync_enabled && var.sso_instance_arn != "" && var.identity_store_id == "" ?
    ["identity_store_id must be provided when sso_instance_arn is provided and attribute_sync_enabled is true"] : [],

    # Validate all rules reference managed groups
    [for rule in var.attribute_sync_rules :
      "Rule for group '${rule.group_name}' references a group not in attribute_sync_managed_groups list"
      if !contains(var.attribute_sync_managed_groups, rule.group_name)
    ],

    # Validate no overlap between attribute_sync_managed_groups and group_config
    # Groups managed by attribute syncer should not be in group_config (used by revoker for JIT access)
    # as this causes false "inconsistent assignment" warnings
    [for group_name in local.group_config_group_names :
      "Group '${group_name}' is in both attribute_sync_managed_groups and group_config. This will cause false 'inconsistent assignment' warnings. Remove it from group_config if it should be managed by attribute syncer."
      if contains(var.attribute_sync_managed_groups, group_name)
    ]
  )

  # Extract group names from group_config (Resource field can be a string or list)
  group_config_group_names = distinct(flatten([
    for stmt in var.group_config : (
      try(tolist(stmt.Resource), [stmt.Resource])
    )
  ]))
}

resource "random_string" "random" {
  length  = 16
  special = false
  upper   = false
  numeric = false
}
