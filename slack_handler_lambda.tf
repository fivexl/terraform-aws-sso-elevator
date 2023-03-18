module "access_requester_slack_handler" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.10.1"

  function_name = local.name
  description   = "Receive requests from slack and grants temporary access"
  handler       = "main.lambda_handler"
  runtime       = "python3.9"
  publish       = true
  timeout       = 30

  source_path = "${path.module}/src/"

  environment_variables = {
    SLACK_SIGNING_SECRET = data.aws_ssm_parameter.slack_signing_secret.value
    SLACK_BOT_TOKEN      = data.aws_ssm_parameter.slack_bot_token.value
    LOG_LEVEL            = "DEBUG" #FIXME
    DYNAMODB_TABLE_NAME  = module.dynamodb_table_requests.dynamodb_table_id
    SLACK_CHANNEL_ID     = "C8GT89CQ0" # FIXME
  }

  create_lambda_function_url = true

  cors = {
    allow_credentials = true
    allow_origins     = ["https://slack.com"]
    allow_methods     = ["POST"]
    max_age           = 86400
  }

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.slack_handler.json

  dead_letter_target_arn    = aws_sns_topic.dlq.arn
  attach_dead_letter_policy = true

  # do not retry automatically
  maximum_retry_attempts = 0

  cloudwatch_logs_retention_in_days = 365

  tags = var.tags
}

data "aws_iam_policy_document" "slack_handler" {
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
    sid       = "AllowStrongAuditLogToDynamo"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [module.dynamodb_table_requests.dynamodb_table_arn]
  }
}
