# Each parameter starts as a placeholder the Lambdas reject (SLACK_SECRET_PLACEHOLDER in
# src/config.py); operators set the real value with `aws ssm put-parameter --overwrite`.
# value_wo is never persisted to state.
# Do not bump value_wo_version or add non-tag arguments: an Update re-puts value_wo and
# overwrites the operator's secret with the placeholder.
resource "aws_ssm_parameter" "slack_bot_token" {
  name             = var.slack_bot_token_ssm_parameter_name
  type             = "SecureString"
  value_wo         = "REPLACE_ME"
  value_wo_version = 1
  tags             = var.tags
}

resource "aws_ssm_parameter" "slack_signing_secret" {
  name             = var.slack_signing_secret_ssm_parameter_name
  type             = "SecureString"
  value_wo         = "REPLACE_ME"
  value_wo_version = 1
  tags             = var.tags
}

# The revoker and attribute-syncer read only the bot token; the access-requester reads both.
locals {
  slack_secret_parameter_arns = {
    bot_token = [aws_ssm_parameter.slack_bot_token.arn]
    all       = [aws_ssm_parameter.slack_bot_token.arn, aws_ssm_parameter.slack_signing_secret.arn]
  }
}

data "aws_iam_policy_document" "read_slack_secrets" {
  for_each = local.slack_secret_parameter_arns

  statement {
    sid       = "AllowReadSlackSecretsFromSSM"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = each.value
  }
  # Resource is "*" because the key may be alias/aws/ssm or a customer managed key the
  # operator chose in put-parameter; the conditions limit it to SSM decrypting these parameters.
  statement {
    sid       = "AllowDecryptSlackSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.${data.aws_partition.current.dns_suffix}"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:PARAMETER_ARN"
      values   = each.value
    }
  }
}
