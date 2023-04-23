module "access_revoker" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.13.0"

  function_name = local.revoker_lambda_name
  description   = "Revokes temporary permissions"
  handler       = "revoker.lambda_handler"
  publish       = true
  timeout       = 300

  hash_extra = local.revoker_lambda_name

  build_in_docker = var.build_in_docker
  runtime         = "python3.9"
  docker_image    = "build-python3.9-poetry"
  docker_file     = "${path.module}/src/docker/Dockerfile"
  source_path = [
    {
      path           = "${path.module}/src/"
      poetry_install = true
      artifacts_dir  = "${path.root}/builds/"
      patterns = [
        "!.venv/.*",
        "!.vscode/.*",
        "!__pycache__/.*",
        "!tests/.*",
      ]
    }
  ]

  environment_variables = {
    LOG_LEVEL = var.log_level

    SLACK_SIGNING_SECRET = var.slack_signing_secret
    SLACK_BOT_TOKEN      = var.slack_bot_token
    SLACK_CHANNEL_ID     = var.slack_channel_id

    DYNAMODB_TABLE_NAME         = module.dynamodb_table_requests.dynamodb_table_id
    SSO_INSTANCE_ARN            = local.sso_instance_arn
    STATEMENTS                  = jsonencode(var.config)
    POWERTOOLS_LOGGER_LOG_EVENT = true

    POST_UPDATE_TO_SLACK  = var.revoker_post_update_to_slack
    SCHEDULE_POLICY_ARN   = aws_iam_role.eventbridge_role.arn
    REVOKER_FUNCTION_ARN  = local.revoker_lambda_arn
    REVOKER_FUNCTION_NAME = local.revoker_lambda_name
  }

  allowed_triggers = {
    cron = {
      principal  = "events.amazonaws.com"
      source_arn = aws_cloudwatch_event_rule.every_night.arn
    }
  }

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.revoker.json

  dead_letter_target_arn    = aws_sns_topic.dlq.arn
  attach_dead_letter_policy = true

  # do not retry automatically
  maximum_retry_attempts = 0

  cloudwatch_logs_retention_in_days = 365

  layers = [
    module.powertools_pydantic.lambda_layer_arn,
    module.slack_bolt.lambda_layer_arn,
    module.python_boto3.lambda_layer_arn,
  ]

  tags = var.tags
}

data "aws_iam_policy_document" "revoker" {
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
    sid       = "AllowStrongAuditLogToDynamo"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [module.dynamodb_table_requests.dynamodb_table_arn]
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
      "scheduler:ListSchedules",
      "scheduler:GetSchedule",
    ]
    resources = ["*"]
  }
}
resource "aws_scheduler_schedule_group" "One_time_schedule_group" {
  name = "SSO_Elevator_revoke"
}

resource "aws_cloudwatch_event_rule" "every_night" {
  name                = "every-night"
  description         = "Trigger every night"
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "revoker" {
  rule = aws_cloudwatch_event_rule.every_night.name
  arn  = module.access_revoker.lambda_function_arn
}

resource "aws_iam_role" "eventbridge_role" {
  name = "EventBridgeRoleForSSOElevator"
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
  function_name = local.revoker_lambda_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_iam_role.eventbridge_role.arn
}
