module "access_revoker" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.10.1"

  function_name = local.revoker_lambda_name
  description   = "Revokes temporary permissions"
  handler       = "revoker.lambda_handler"
  runtime       = "python3.9"
  publish       = true
  timeout       = 300

  hash_extra = local.revoker_lambda_name
  source_path = [
    {
      path           = "${path.module}/src/"
      poetry_install = true
      artifacts_dir  = "${path.root}/builds/"
      patterns = [
        "!.venv/.*",
      ]
    }
  ]

  environment_variables = {
    SLACK_BOT_TOKEN             = var.slack_bot_token
    LOG_LEVEL                   = var.log_level
    DYNAMODB_TABLE_NAME         = module.dynamodb_table_requests.dynamodb_table_id
    SLACK_CHANNEL_ID            = var.slack_channel_id
    SSO_INSTANCE_ARN            = local.sso_instance_arn
    CONFIG                      = jsonencode(var.config)
    POWERTOOLS_LOGGER_LOG_EVENT = true
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
