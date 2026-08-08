module "access_requester_slack_handler" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = var.requester_lambda_name
  description   = "Receive requests from slack and grants temporary access"

  publish       = true
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory_size
  architectures = [var.lambda_architecture]

  # Pull image from ecr
  package_type   = var.use_pre_created_image ? "Image" : "Zip"
  create_package = var.use_pre_created_image ? false : true
  image_uri      = var.use_pre_created_image ? "${var.ecr_owner_account_id}.dkr.ecr.${data.aws_region.current.region}.amazonaws.com/${var.ecr_repo_name}:requester-${var.ecr_repo_tag}" : null

  # Build zip from source code using Docker
  hash_extra      = var.use_pre_created_image ? "" : var.requester_lambda_name
  handler         = var.use_pre_created_image ? "" : "main.lambda_handler"
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

  # The configuration lives in SSM Parameter Store (see requester_ssm_parameters.tf).
  # Only the bootstrap values that are read before the config is loaded stay here:
  # LOG_LEVEL is consumed by config.get_logger() at import time and
  # POWERTOOLS_LOGGER_LOG_EVENT is read by aws-lambda-powertools itself.
  environment_variables = {
    LOG_LEVEL                   = var.log_level
    POWERTOOLS_LOGGER_LOG_EVENT = true

    SSM_CONFIG_PARAMETER_PATH = local.requester_ssm_parameter_path
    SSM_CONFIG_VERSION        = local.requester_ssm_config_version
  }

  allowed_triggers = var.create_api_gateway ? {
    AllowExecutionFromAPIGateway = {
      service    = "apigateway"
      source_arn = "${module.http_api[0].api_execution_arn}/*/*${local.api_resource_path}"
    }
  } : {}

  create_lambda_function_url = var.create_lambda_url ? true : false

  cors = var.create_lambda_url ? {
    allow_credentials = true
    allow_origins     = ["https://slack.com"]
    allow_methods     = ["POST"]
    max_age           = 86400
  } : null

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.slack_handler.json

  dead_letter_target_arn    = var.aws_sns_topic_subscription_email != "" ? aws_sns_topic.dlq[0].arn : null
  attach_dead_letter_policy = var.aws_sns_topic_subscription_email != "" ? true : false

  # do not retry automatically
  maximum_retry_attempts = 0

  cloudwatch_logs_retention_in_days = var.logs_retention_in_days

  tags = var.tags
}

# By default, the same policy is created by the "aws_lambda_function_url" resource
# But for reason i was not able to find out, in some cases of creation with the "API Gateway" resource, the policy is not created
# So we are creating the same policy but using the "aws_lambda_permission" resource.
resource "aws_lambda_permission" "url" {
  count                  = var.create_lambda_url ? 1 : 0
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.access_requester_slack_handler.lambda_function_name
  principal              = "*"
  statement_id           = "AllowExecutionFromLambdaURL"
  function_url_auth_type = "NONE"
  # Adds the following condition keys, which are required for the function to be invoked from a URL:
  # "Condition": {
  #      "StringEquals": {
  #        "lambda:FunctionUrlAuthType": "None"
  #      }
  #    }
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
  statement {
    effect = "Allow"
    actions = [
      "identitystore:ListGroups",
      "identitystore:DescribeGroup",
      "identitystore:ListGroupMemberships",
      "identitystore:ListGroupMembershipsForMember",
      "identitystore:CreateGroupMembership",
    ]
    resources = ["*"]
  }
  statement {
    sid    = "AllowS3Config"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      module.config_bucket.s3_bucket_arn,
      "${module.config_bucket.s3_bucket_arn}/*"
    ]
  }
  statement {
    sid    = "AllowReadConfigFromSSM"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.requester_ssm_parameter_path}",
      "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.requester_ssm_parameter_path}/*",
    ]
  }

  # Needed to read the SecureString parameters. Scoped by kms:ViaService because the AWS managed
  # alias/aws/ssm key cannot be referenced by ARN, and it is not known which key the caller passed.
  statement {
    sid    = "AllowDecryptSSMParameters"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }
}

module "http_api" {
  count         = var.create_api_gateway ? 1 : 0
  source        = "terraform-aws-modules/apigateway-v2/aws"
  version       = "5.0.0"
  name          = var.api_gateway_name
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
        uri  = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.requester_lambda_name}"
        type = "AWS_PROXY"
      }
      throttling_burst_limit = var.api_gateway_throttling_burst_limit
      throttling_rate_limit  = var.api_gateway_throttling_rate_limit
    }
  }
  stage_name         = local.api_stage_name
  create_domain_name = false
  tags               = var.tags
  stage_access_log_settings = {
    create_log_group            = true
    log_group_retention_in_days = var.logs_retention_in_days
  }
}
