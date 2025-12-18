# Attribute Syncer Lambda Function
# This Lambda function automatically synchronizes users to groups based on their attributes.
# It runs on a configurable schedule and integrates with the existing SSO Elevator infrastructure.

module "attribute_syncer" {
  count   = var.attribute_sync_enabled ? 1 : 0
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = var.attribute_syncer_lambda_name
  description   = "Automatically synchronizes users to groups based on their attributes"

  publish       = true
  timeout       = var.attribute_sync_lambda_timeout
  memory_size   = var.attribute_sync_lambda_memory
  architectures = [var.lambda_architecture]

  # Pull image from ecr
  package_type   = var.use_pre_created_image ? "Image" : "Zip"
  create_package = var.use_pre_created_image ? false : true
  image_uri      = var.use_pre_created_image ? "${var.ecr_owner_account_id}.dkr.ecr.${data.aws_region.current.region}.amazonaws.com/${var.ecr_repo_name}:attribute-syncer-${var.ecr_repo_tag}" : null

  # Build zip from source code using Docker
  hash_extra      = var.use_pre_created_image ? "" : var.attribute_syncer_lambda_name
  handler         = var.use_pre_created_image ? "" : "attribute_syncer.lambda_handler"
  runtime         = var.use_pre_created_image ? "" : "python${local.python_version}"
  build_in_docker = var.use_pre_created_image ? false : true
  source_path = var.use_pre_created_image ? [] : [
    {
      path             = "${path.module}/src/"
      artifacts_dir    = "${path.root}/builds/"
      pip_requirements = "${path.module}/src/requirements.txt"
      patterns = [
        "!.venv/.*",
        "!.vscode/.*",
        "!__pycache__/.*",
        "!tests/.*",
        "!tools/.*",
        "!.hypothesis/.*",
        "!.pytest_cache/.*",
        "!uv.lock",
        "!pyproject.toml",
      ]
    }
  ]

  layers = var.use_pre_created_image ? [] : [
    module.sso_elevator_dependencies[0].lambda_layer_arn,
  ]

  environment_variables = {
    LOG_LEVEL = var.log_level

    SLACK_BOT_TOKEN  = var.slack_bot_token
    SLACK_CHANNEL_ID = var.slack_channel_id

    SSO_INSTANCE_ARN            = local.sso_instance_arn
    IDENTITY_STORE_ID           = local.identity_store_id
    POWERTOOLS_LOGGER_LOG_EVENT = true

    S3_BUCKET_FOR_AUDIT_ENTRY_NAME  = local.s3_bucket_name
    S3_BUCKET_PREFIX_FOR_PARTITIONS = var.s3_bucket_partition_prefix

    # Attribute sync specific configuration
    ATTRIBUTE_SYNC_ENABLED                  = "true"
    ATTRIBUTE_SYNC_MANAGED_GROUPS           = jsonencode(var.attribute_sync_managed_groups)
    ATTRIBUTE_SYNC_RULES                    = jsonencode(var.attribute_sync_rules)
    ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY = var.attribute_sync_manual_assignment_policy
    ATTRIBUTE_SYNC_SCHEDULE                 = var.attribute_sync_schedule
  }

  allowed_triggers = {
    attribute_sync_schedule = {
      principal  = "events.amazonaws.com"
      source_arn = aws_cloudwatch_event_rule.attribute_sync_schedule[0].arn
    }
  }

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.attribute_syncer[0].json

  dead_letter_target_arn    = var.aws_sns_topic_subscription_email != "" ? aws_sns_topic.dlq[0].arn : null
  attach_dead_letter_policy = var.aws_sns_topic_subscription_email != "" ? true : false

  # do not retry automatically
  maximum_retry_attempts = 0

  cloudwatch_logs_retention_in_days = var.logs_retention_in_days

  tags = var.tags
}

# IAM Policy for Attribute Syncer Lambda
data "aws_iam_policy_document" "attribute_syncer" {
  count = var.attribute_sync_enabled ? 1 : 0

  statement {
    sid    = "AllowListSSOInstances"
    effect = "Allow"
    actions = [
      "sso:ListInstances"
    ]
    resources = ["*"]
  }

  # Identity Store permissions for reading users and groups
  statement {
    sid    = "AllowIdentityStoreRead"
    effect = "Allow"
    actions = [
      "identitystore:ListUsers",
      "identitystore:DescribeUser",
      "identitystore:ListGroups",
      "identitystore:DescribeGroup",
      "identitystore:ListGroupMemberships",
    ]
    resources = ["*"]
  }

  # Identity Store permissions for managing group memberships
  statement {
    sid    = "AllowIdentityStoreWrite"
    effect = "Allow"
    actions = [
      "identitystore:CreateGroupMembership",
      "identitystore:DeleteGroupMembership",
    ]
    resources = ["*"]
  }

  # S3 permissions for audit logging
  statement {
    sid    = "AllowS3AuditWrite"
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = ["${local.s3_bucket_arn}/${var.s3_bucket_partition_prefix}/*"]
  }
}

# EventBridge Schedule Rule for Attribute Sync
resource "aws_cloudwatch_event_rule" "attribute_sync_schedule" {
  count               = var.attribute_sync_enabled ? 1 : 0
  name                = local.attribute_sync_event_rule_name
  description         = "Triggers attribute syncer Lambda on schedule to sync users to groups based on attributes"
  schedule_expression = var.attribute_sync_schedule
  tags                = var.tags
}

# EventBridge Target for Attribute Sync
resource "aws_cloudwatch_event_target" "attribute_sync_schedule" {
  count = var.attribute_sync_enabled ? 1 : 0
  rule  = aws_cloudwatch_event_rule.attribute_sync_schedule[0].name
  arn   = module.attribute_syncer[0].lambda_function_arn
  input = jsonencode({
    "action" : "attribute_sync"
  })
}

# Terraform validation for attribute sync configuration
resource "null_resource" "attribute_sync_validation" {
  count = length(local.attribute_sync_validation_errors) > 0 ? 1 : 0

  triggers = {
    validation_errors = join(", ", local.attribute_sync_validation_errors)
  }

  provisioner "local-exec" {
    command = "echo 'Attribute sync validation errors: ${join(", ", local.attribute_sync_validation_errors)}' && exit 1"
  }
}
