module "access_requester_slack_handler" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.16.0"

  function_name = var.requester_lambda_name
  description   = "Receive requests from slack and grants temporary access"

  publish = true
  timeout = 30

  # Pull image from ecr
  package_type   = var.use_pre_created_image ? "Image" : "Zip"
  create_package = var.use_pre_created_image ? false : true
  image_uri      = var.use_pre_created_image ? "${var.ecr_owner_account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com/aws-sso-elevator:requester-aa4c9c4edbd942c328e6d91f9611dd51d4122407" : null

  # Build zip from source code using Docker
  hash_extra      = var.use_pre_created_image ? "" : var.requester_lambda_name
  handler         = var.use_pre_created_image ? "" : "main.lambda_handler"
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


    SSO_INSTANCE_ARN                            = local.sso_instance_arn
    STATEMENTS                                  = jsonencode(var.config)
    POWERTOOLS_LOGGER_LOG_EVENT                 = true
    SCHEDULE_POLICY_ARN                         = aws_iam_role.eventbridge_role.arn
    REVOKER_FUNCTION_ARN                        = local.revoker_lambda_arn
    REVOKER_FUNCTION_NAME                       = var.revoker_lambda_name
    S3_BUCKET_FOR_AUDIT_ENTRY_NAME              = local.s3_bucket_name
    S3_BUCKET_PREFIX_FOR_PARTITIONS             = var.s3_bucket_partition_prefix
    SSO_ELEVATOR_SCHEDULED_REVOCATION_RULE_NAME = aws_cloudwatch_event_rule.sso_elevator_scheduled_revocation.name
    REQUEST_EXPIRATION_HOURS                    = var.request_expiration_hours
    APPROVER_RENOTIFICATION_INITIAL_WAIT_TIME   = var.approver_renotification_initial_wait_time
    APPROVER_RENOTIFICATION_BACKOFF_MULTIPLIER  = var.approver_renotification_backoff_multiplier
    MAX_PERMISSIONS_DURATION_TIME               = var.max_permissions_duration_time
  }

  allowed_triggers = {
    AllowExecutionFromAPIGateway = {
      service    = "apigateway"
      source_arn = "${module.http_api.api_execution_arn}/*/*${local.api_resource_path}"
    }
  }

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.slack_handler.json

  dead_letter_target_arn    = var.aws_sns_topic_subscription_email != "" ? aws_sns_topic.dlq[0].arn : null
  attach_dead_letter_policy = var.aws_sns_topic_subscription_email != "" ? true : false

  # do not retry automatically
  maximum_retry_attempts = 0

  cloudwatch_logs_retention_in_days = 365

  tags = var.tags
}

data "aws_iam_policy_document" "slack_handler" {
  statement {
    sid    = "GetSAMLProvider"
    effect = "Allow"
    actions = [
      "iam:GetSAMLProvider"
    ]
    resources = ["*"]
  }
  statement {
    sid    = "UpdateSAMLProvider"
    effect = "Allow"
    actions = [
      "iam:UpdateSAMLProvider",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "GetInvokeSelf"
    effect = "Allow"
    actions = [
      "lambda:InvokeFunction",
      "lambda:GetFunction"
    ]
    resources = [local.requester_lambda_arn]
  }
  statement {
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = ["${local.s3_bucket_arn}/${var.s3_bucket_partition_prefix}/*"]
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
      "sso:CreateAccountAssignment",
      "sso:DescribeAccountAssignmentCreationStatus"
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
      "iam:PutRolePolicy",
      "iam:AttachRolePolicy",
      "iam:CreateRole",
      "iam:GetRole",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
    ]
    resources = [
      "arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_*",
      "arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_*"
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
      "scheduler:CreateSchedule",
      "iam:PassRole",
      "scheduler:ListSchedules",
      "scheduler:GetSchedule",
      "scheduler:DeleteSchedule",
    ]
    resources = ["*"]
  }
}

module "http_api" {
  source        = "terraform-aws-modules/apigateway-v2/aws"
  version       = "5.0.0"
  name          = "sso-elevator-access-requster"
  description   = "API Gateway for SSO Elevator's access-requester Lambda, to communicate with Slack"
  protocol_type = "HTTP"

  cors_configuration = {
    allow_credentials = true
    allow_origins     = ["https://slack.com"]
    allow_methods     = ["POST"]
    max_age           = 86400
  }

  routes = {
    "POST ${local.api_resource_path}" : {
      integration = {
        uri  = module.access_requester_slack_handler.lambda_function_arn
        type = "AWS_PROXY"
      }
    }
  }
  stage_name         = local.api_stage_name
  create_domain_name = false
}
