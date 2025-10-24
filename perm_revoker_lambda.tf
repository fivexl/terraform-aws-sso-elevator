module "access_revoker" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "7.19.0"

  function_name = var.revoker_lambda_name
  description   = "Revokes temporary permissions"

  publish     = true
  timeout     = var.lambda_timeout
  memory_size = var.lambda_memory_size

  # Pull image from ecr
  package_type   = var.use_pre_created_image ? "Image" : "Zip"
  create_package = var.use_pre_created_image ? false : true
  image_uri      = var.use_pre_created_image ? "${var.ecr_owner_account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com/${var.ecr_repo_name}:revoker-${var.ecr_repo_tag}" : null

  # Build zip from source code using Docker
  hash_extra      = var.use_pre_created_image ? "" : var.revoker_lambda_name
  handler         = var.use_pre_created_image ? "" : "revoker.lambda_handler"
  runtime         = var.use_pre_created_image ? "" : "python${local.python_version}"
  build_in_docker = var.use_pre_created_image ? false : true
  docker_image    = var.use_pre_created_image ? null : "lambda/python:${local.python_version}"
  docker_file     = var.use_pre_created_image ? null : "${path.module}/src/docker/Dockerfile"
  source_path = var.use_pre_created_image ? [] : [
    {
      path             = "${path.module}/src/"
      pip_requirements = "${path.module}/src/deploy_requirements.txt"
      artifacts_dir    = "${path.root}/builds/"
      patterns = [
        "!.venv/.*",
        "!.vscode/.*",
        "!__pycache__/.*",
        "!tests/.*",
        "!tools/.*",
        "!.hypothesis/.*",
        "!.pytest_cache/.*",
      ]
    }
  ]

  layers = var.use_pre_created_image ? [] : [
    module.sso_elevator_dependencies[0].lambda_layer_arn,
  ]

  environment_variables = {
    LOG_LEVEL = var.log_level

    SLACK_SIGNING_SECRET = var.slack_signing_secret
    SLACK_BOT_TOKEN      = var.slack_bot_token
    SLACK_CHANNEL_ID     = var.slack_channel_id
    SCHEDULE_GROUP_NAME  = var.schedule_group_name

    SSO_INSTANCE_ARN            = local.sso_instance_arn
    STATEMENTS                  = jsonencode(var.config)
    GROUP_STATEMENTS            = jsonencode(var.group_config)
    POWERTOOLS_LOGGER_LOG_EVENT = true

    POST_UPDATE_TO_SLACK                        = var.revoker_post_update_to_slack
    SCHEDULE_POLICY_ARN                         = aws_iam_role.eventbridge_role.arn
    REVOKER_FUNCTION_ARN                        = local.revoker_lambda_arn
    REVOKER_FUNCTION_NAME                       = var.revoker_lambda_name
    S3_BUCKET_FOR_AUDIT_ENTRY_NAME              = local.s3_bucket_name
    S3_BUCKET_PREFIX_FOR_PARTITIONS             = var.s3_bucket_partition_prefix
    SSO_ELEVATOR_SCHEDULED_REVOCATION_RULE_NAME = aws_cloudwatch_event_rule.sso_elevator_scheduled_revocation.name
    REQUEST_EXPIRATION_HOURS                    = var.request_expiration_hours
    MAX_PERMISSIONS_DURATION_TIME               = var.max_permissions_duration_time
    PERMISSION_DURATION_LIST_OVERRIDE           = jsonencode(var.permission_duration_list_override)

    APPROVER_RENOTIFICATION_INITIAL_WAIT_TIME  = var.approver_renotification_initial_wait_time
    APPROVER_RENOTIFICATION_BACKOFF_MULTIPLIER = var.approver_renotification_backoff_multiplier
    SECONDARY_FALLBACK_EMAIL_DOMAINS           = jsonencode(var.secondary_fallback_email_domains)
    SEND_DM_IF_USER_NOT_IN_CHANNEL             = var.send_dm_if_user_not_in_channel
    CACHE_TABLE_NAME                           = var.cache_table_name
    CACHE_TTL_MINUTES                          = var.cache_ttl_minutes
  }

  allowed_triggers = {
    cron = {
      principal  = "events.amazonaws.com"
      source_arn = aws_cloudwatch_event_rule.sso_elevator_scheduled_revocation.arn
    }
    check_inconsistency = {
      principal  = "events.amazonaws.com"
      source_arn = aws_cloudwatch_event_rule.sso_elevator_check_on_inconsistency.arn
    }
  }

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.revoker.json

  dead_letter_target_arn    = var.aws_sns_topic_subscription_email != "" ? aws_sns_topic.dlq[0].arn : null
  attach_dead_letter_policy = var.aws_sns_topic_subscription_email != "" ? true : false

  # do not retry automatically
  maximum_retry_attempts = 0

  cloudwatch_logs_retention_in_days = var.logs_retention_in_days

  tags = var.tags
}

data "aws_iam_policy_document" "revoker" {
  statement {
    sid    = "AllowDescribeRule"
    effect = "Allow"
    actions = [
      "events:DescribeRule"
    ]
    resources = [
      "arn:aws:events:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:rule/${local.event_bridge_scheduled_revocation_rule_name}"
    ]
  }
  statement {
    sid    = "AllowListSSOInstances"
    effect = "Allow"
    actions = [
      "sso:ListInstances"
    ]
    resources = ["*"]
  }
  statement {
    sid    = "AllowSSO"
    effect = "Allow"
    actions = [
      "sso:ListAccountAssignments",
      "sso:DeleteAccountAssignment",
      "sso:DescribeAccountAssignmentDeletionStatus"
    ]
    resources = [
      "arn:aws:sso:::instance/*",
      "arn:aws:sso:::permissionSet/*/*",
      "arn:aws:sso:::account/*"
    ]
  }
  statement {
    effect = "Allow"
    actions = [
      "organizations:ListAccounts",
      "organizations:DescribeAccount",
      "sso:ListPermissionSets",
      "sso:DescribePermissionSet",
      "identitystore:ListUsers",
      "identitystore:DescribeUser",
    ]
    resources = ["*"]
  }
  statement {
    effect = "Allow"
    actions = [
      "scheduler:DeleteSchedule",
      "iam:PassRole",
      "scheduler:CreateSchedule",
      "scheduler:ListSchedules",
      "scheduler:GetSchedule",
    ]
    resources = ["*"]
  }
  statement {
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = ["${local.s3_bucket_arn}/${var.s3_bucket_partition_prefix}/*"]
  }
  statement {
    effect = "Allow"
    actions = [
      "identitystore:ListGroups",
      "identitystore:DescribeGroup",
      "identitystore:ListGroupMemberships",
      "identitystore:DeleteGroupMembership"
    ]
    resources = ["*"]
  }
  statement {
    sid    = "AllowDynamoDBCache"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.sso_elevator_cache.arn]
  }
}

resource "aws_cloudwatch_event_rule" "sso_elevator_scheduled_revocation" {
  name                = local.event_bridge_scheduled_revocation_rule_name
  description         = "Triggers on schedule to revoke temporary permissions."
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "sso_elevator_scheduled_revocation" {
  rule = aws_cloudwatch_event_rule.sso_elevator_scheduled_revocation.name
  arn  = module.access_revoker.lambda_function_arn
  input = jsonencode({
    "action" : "sso_elevator_scheduled_revocation"
  })
}

resource "aws_cloudwatch_event_rule" "sso_elevator_check_on_inconsistency" {
  name                = local.event_bridge_check_on_inconsistency_rule_name
  description         = "Triggers on schedule to check on inconsistency."
  schedule_expression = var.schedule_expression_for_check_on_inconsistency
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "check_inconsistency" {
  rule = aws_cloudwatch_event_rule.sso_elevator_check_on_inconsistency.name
  arn  = module.access_revoker.lambda_function_arn
  input = jsonencode({
    "action" : "check_on_inconsistency"
  })
}

resource "aws_iam_role" "eventbridge_role" {
  name = var.schedule_role_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      },
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "scheduler.amazonaws.com"
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "eventbridge_policy" {
  name = "eventbridge_policy_for_sso_elevator"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "events:PutRule",
          "events:PutTargets"
        ]
        Effect   = "Allow"
        Resource = "*"
      },
      {
        Action = [
          "lambda:InvokeFunction"
        ]
        Effect   = "Allow"
        Resource = module.access_revoker.lambda_function_arn
      }
    ]
  })

  role = aws_iam_role.eventbridge_role.id
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = module.access_revoker.lambda_function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_iam_role.eventbridge_role.arn
}
