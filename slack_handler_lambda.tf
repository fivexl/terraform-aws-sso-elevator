module "access_requester_slack_handler" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.16.0"

  function_name = local.requester_lambda_name
  description   = "Receive requests from slack and grants temporary access"
  handler       = "main.lambda_handler"
  publish       = true
  timeout       = 30

  build_in_docker = var.build_in_docker
  runtime         = "python3.10"
  docker_image    = "build-python3.10-poetry"
  docker_file     = "${path.module}/src/docker/Dockerfile"
  hash_extra      = local.requester_lambda_name
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
    SCHEDULE_POLICY_ARN         = aws_iam_role.eventbridge_role.arn
    REVOKER_FUNCTION_ARN        = local.revoker_lambda_arn
    REVOKER_FUNCTION_NAME       = local.revoker_lambda_name
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

  layers = [
    module.powertools_pydantic.lambda_layer_arn,
    module.slack_bolt.lambda_layer_arn,
    module.python_boto3.lambda_layer_arn,
  ]

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
  statement {
    effect = "Allow"
    actions = [
      "iam:PutRolePolicy", "iam:CreateRole", "iam:GetRole", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies"
    ]
    resources = ["arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_*"]
  }
  statement {
    effect = "Allow"
    actions = [
      "iam:PutRolePolicy", "iam:CreateRole", "iam:GetRole", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies"
    ]
    resources = ["arn:aws:iam::*:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_*"]
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


