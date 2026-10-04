module "access_requester_slack_handler" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.8.2"

  function_name = var.requester_lambda_name
  description   = "Receive requests from slack and grants temporary access"

  publish       = true
  snap_start    = var.snap_start
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

  environment_variables = merge(
    {
      LOG_LEVEL = var.log_level

      SLACK_BOT_TOKEN_SSM_PARAMETER_NAME      = aws_ssm_parameter.slack_bot_token.name
      SLACK_SIGNING_SECRET_SSM_PARAMETER_NAME = aws_ssm_parameter.slack_signing_secret.name
      SLACK_CHANNEL_ID                        = var.slack_channel_id
      SCHEDULE_GROUP_NAME                     = var.schedule_group_name

      SSO_INSTANCE_ARN                            = local.sso_instance_arn
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
      PERMISSION_DURATION_LIST_OVERRIDE           = jsonencode(var.permission_duration_list_override)
      SECONDARY_FALLBACK_EMAIL_DOMAINS            = jsonencode(var.secondary_fallback_email_domains)
      SEND_DM_IF_USER_NOT_IN_CHANNEL              = var.send_dm_if_user_not_in_channel
      CONFIG_BUCKET_NAME                          = local.config_bucket_name
      CONFIG_S3_KEY                               = "config/approval-config.json"
      CACHE_ENABLED                               = var.cache_enabled
      # "" when unset, not a real null -- Lambda environment variables can't
      # carry one. src/config.py's config_bucket_kms_key_arn field uses the
      # same empty-string sentinel already established for
      # cli_expected_api_id. Lets the account/permission-set/user caches
      # this Lambda writes into this same bucket use the operator's own KMS
      # key too, instead of always hardcoding AES256 regardless of what
      # encryption the bucket's other object (approval-config.json, below)
      # already uses (#194 High #5, found by Andrey Devyatkin).
      CONFIG_BUCKET_KMS_KEY_ARN = var.config_bucket_kms_key_arn != null ? var.config_bucket_kms_key_arn : ""
    },
    var.enable_access_requester_cli ? {
      # The audience the CLI's STS identity proof must name: see src/config.py's cli_expected_api_id.
      CLI_EXPECTED_API_ID = aws_api_gateway_rest_api.requester.id
    } : {}
  )

  # API Gateway invokes the "live" alias, so its permissions are aws_lambda_permission.api_gateway.
  create_current_version_allowed_triggers   = false
  create_unqualified_alias_allowed_triggers = false

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.slack_handler.json

  dead_letter_target_arn    = var.aws_sns_topic_subscription_email != "" ? aws_sns_topic.dlq[0].arn : null
  attach_dead_letter_policy = var.aws_sns_topic_subscription_email != "" ? true : false

  cloudwatch_logs_retention_in_days = var.logs_retention_in_days

  tags = var.tags
}

# API Gateway targets this alias rather than $LATEST, so a new version goes live only once the
# alias moves to it -- the hook SnapStart and provisioned concurrency need.
module "access_requester_alias" {
  source  = "terraform-aws-modules/lambda/aws//modules/alias"
  version = "8.8.2"

  name             = local.requester_alias_name
  function_name    = module.access_requester_slack_handler.lambda_function_name
  function_version = module.access_requester_slack_handler.lambda_function_version
  refresh_alias    = true

  # Permissions are aws_lambda_permission.api_gateway below: the submodule's qualified-alias
  # permissions don't depend on its alias, so a fresh install can race "alias not found".
  create_version_allowed_triggers         = false
  create_qualified_alias_allowed_triggers = false
}

# One permission per route on the alias, scoped to its stage and method.
resource "aws_lambda_permission" "api_gateway" {
  for_each = merge(
    { AllowExecutionFromAPIGateway = local.slack_method_arn },
    var.enable_access_requester_cli ? { AllowExecutionFromAPIGatewayCli = local.cli_method_arn } : {}
  )

  statement_id  = each.key
  action        = "lambda:InvokeFunction"
  function_name = module.access_requester_slack_handler.lambda_function_name
  qualifier     = module.access_requester_alias.lambda_alias_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = each.value
}

# Bolt lazy listeners re-invoke the alias asynchronously; a retry would repeat their Slack posts
# and grants. Standalone because the module applies maximum_retry_attempts only with
# create_async_event_config, and the alias submodule's async config can't target only the alias.
resource "aws_lambda_function_event_invoke_config" "access_requester_live" {
  function_name          = module.access_requester_slack_handler.lambda_function_name
  qualifier              = module.access_requester_alias.lambda_alias_name
  maximum_retry_attempts = 0
}

data "aws_iam_policy_document" "slack_handler" {
  source_policy_documents = [
    data.aws_iam_policy_document.read_slack_secrets["all"].json,
    data.aws_iam_policy_document.schedule_access.json,
  ]

  # Identity Center's own SAML provider, which it updates when creating an assignment in the
  # management account (AWS docs: AccessToSSOProvisionedRoles).
  statement {
    sid    = "SSOSAMLProvider"
    effect = "Allow"
    actions = [
      "iam:GetSAMLProvider",
      "iam:UpdateSAMLProvider",
    ]
    resources = ["arn:aws:iam::*:saml-provider/AWSSSO_*_DO_NOT_DELETE"]
  }

  # Bolt lazy listeners invoke context.invoked_function_arn, the "live" alias. Built from the
  # local string: referencing the alias resource would make this policy depend on the function.
  statement {
    sid    = "GetInvokeSelf"
    effect = "Allow"
    actions = [
      "lambda:InvokeFunction",
      "lambda:GetFunction"
    ]
    resources = ["${local.requester_lambda_arn}:${local.requester_alias_name}"]
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
      "sso:DescribeAccountAssignmentCreationStatus",
    ]
    resources = [
      "arn:aws:sso:::instance/*",
      "arn:aws:sso:::permissionSet/*/*",
      "arn:aws:sso:::account/*"
    ]
  }
  # IAM Identity Center provisions the AWSReservedSSO_ role with the caller's permissions when it
  # creates an account assignment in the management account (AWS docs: AccessToSSOProvisionedRoles).
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
  # identitystore:ListUsers maps a CLI caller's session name to an Identity Store user (src/cli_auth.py).
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
      "s3:ListBucket",
    ]
    resources = [
      module.config_bucket.s3_bucket_arn,
      "${module.config_bucket.s3_bucket_arn}/*"
    ]
  }
  # Read (above) still covers the whole bucket -- this Lambda genuinely
  # needs to read both config/approval-config.json and every cache object.
  # Write is scoped to only the cache key shapes cache.py's CacheKey
  # constants define (accounts.json, permission_sets/*, users/*), not
  # config/approval-config.json itself: that file is Terraform-managed
  # (aws_s3_object.approval_config), never written by this Lambda at
  # runtime, and a blanket PutObject on the whole bucket meant this
  # Lambda could, in principle, rewrite its own approval rules -- the
  # revoker Lambda's equivalent statement is correctly read-only, since it
  # never writes here at all (#194 note, found by Andrey Devyatkin: newly
  # load-bearing now that #198 also put identity data in this same
  # bucket).
  statement {
    sid    = "AllowS3CacheWrite"
    effect = "Allow"
    actions = [
      "s3:PutObject",
    ]
    resources = [
      "${module.config_bucket.s3_bucket_arn}/accounts.json",
      "${module.config_bucket.s3_bucket_arn}/permission_sets/*",
      "${module.config_bucket.s3_bucket_arn}/users/*",
    ]
  }
  # Only granted when an operator actually configured their own key --
  # kms:GenerateDataKey and kms:Decrypt are what a PutObject/GetObject using
  # SSEKMSKeyId actually needs; without this statement, set_cached_* would
  # 403 on every write once CONFIG_BUCKET_KMS_KEY_ARN is set, since the
  # AllowS3Config statement above only covers S3 actions, not the KMS calls
  # S3 makes on this Lambda's behalf to use that key (#194 High #5, found
  # by Andrey Devyatkin).
  dynamic "statement" {
    for_each = var.config_bucket_kms_key_arn != null ? [var.config_bucket_kms_key_arn] : []
    content {
      sid    = "AllowConfigBucketKMS"
      effect = "Allow"
      actions = [
        "kms:GenerateDataKey",
        "kms:Decrypt",
      ]
      resources = [statement.value]
    }
  }
}
