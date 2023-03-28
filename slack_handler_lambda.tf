module "access_requester_slack_handler" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.10.1"

  function_name = local.requester_lambda_name
  description   = "Receive requests from slack and grants temporary access"
  handler       = "main.lambda_handler"
  runtime       = "python3.9"
  publish       = true
  timeout       = 30

  hash_extra = local.requester_lambda_name
  source_path = [
    {
      path           = "${path.module}/sso-elevator/"
      poetry_install = true
      artifacts_dir  = "${path.root}/builds/"
      patterns = [
        "!.venv/.*",
      ]
    }
  ]

  environment_variables = {
    SLACK_SIGNING_SECRET = var.slack_signing_secret
    SLACK_BOT_TOKEN      = var.slack_bot_token
    LOG_LEVEL            = var.log_level
    DYNAMODB_TABLE_NAME  = module.dynamodb_table_requests.dynamodb_table_id
    SLACK_CHANNEL_ID     = var.slack_channel_id
    CONFIG               = local.sso_elevator_config
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
  statement {
    sid    = "GetSAMLProvider"
    effect = "Allow"
    actions = [
      "iam:GetSAMLProvider"
    ]
    resources = [
      var.identity_provider_arn
    ]
  }

  dynamic "statement" {
    for_each = local.allow_access_to_roles_with_names
    content {
      effect = "Allow"
      actions = [
        "iam:PutRolePolicy", "iam:CreateRole", "iam:GetRole", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies"
      ]
      resources = [statement.value]
    }
  }
}

data "aws_region" "current" {}

locals {
  account_ids           = [for account in var.config.accounts : account.id]
  permission_sets_names = [for permission_set in var.config.permission_sets : permission_set.name]

  account_ids_permission_sets_combinations = flatten([
    for account_id in local.account_ids : [
      for permission_set_name in local.permission_sets_names : {
        account_id          = account_id
        permission_set_name = permission_set_name
      }
    ]
  ])

  allow_access_to_roles_with_names = [
    for v in local.account_ids_permission_sets_combinations :
  "arn:aws:iam::${v.account_id}:role/aws-reserved/sso.amazonaws.com/${data.aws_region.current.name}/AWSReservedSSO_${v.permission_set_name}_*"]
}

locals {
  user_emails = toset(var.config.users[*].email)
}

locals {
  filter_by_user_emails = { for k in local.user_emails : k => { attribute_path = "UserName", attribute_value = k } }
}

data "aws_ssoadmin_instances" "all" {}

locals {
  identity_store_id = tolist(data.aws_ssoadmin_instances.all.identity_store_ids)[0] # TODO: is there always only one? 
}

data "aws_identitystore_user" "all" {
  for_each          = local.filter_by_user_emails
  identity_store_id = local.identity_store_id

  alternate_identifier {
    unique_attribute {
      attribute_path  = each.value.attribute_path
      attribute_value = each.value.attribute_value
    }
  }
}

locals {
  names_of_permission_sets = toset(var.config.permission_sets[*].name)
}

data "aws_ssoadmin_permission_set" "all" {
  instance_arn = tolist(data.aws_ssoadmin_instances.all.arns)[0]
  for_each     = local.names_of_permission_sets
  name         = each.key
}
