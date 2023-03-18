module "access_revoker" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.10.1"

  function_name = "access-revoker"
  description   = "Revokes temporary permissions"
  handler       = "revoker.lambda_handler"
  runtime       = "python3.9"
  publish       = true
  timeout       = 300

  source_path = "${path.module}/src/"

  environment_variables = {
    SLACK_BOT_TOKEN     = data.aws_ssm_parameter.slack_bot_token.value
    LOG_LEVEL           = "INFO" # FIXME
    DYNAMODB_TABLE_NAME = module.dynamodb_table_requests.dynamodb_table_id
    SLACK_CHANNEL_ID    = "C8GT89CQ0" # FIXME
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
  schedule_expression = "cron(0 23 * * ? *)" # FIXME
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "revoker" {
  rule = aws_cloudwatch_event_rule.every_night.name
  arn  = module.access_revoker.lambda_function_arn
}
