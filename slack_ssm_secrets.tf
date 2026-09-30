# Slack secrets in SSM Parameter Store -- see read_slack_secrets_from_ssm in vars.tf.
#
# When read_slack_secrets_from_ssm is enabled, Terraform pre-creates each parameter with a
# placeholder value, using the value_wo/value_wo_version write-only arguments rather than a
# plain value. This is deliberately different from an earlier, rejected version of this
# design: a normal aws_ssm_parameter resource still has its value read back into state on
# every future refresh, even with lifecycle.ignore_changes on value (the AWS provider calls
# GetParameter with decryption to refresh state regardless of that lifecycle rule -- a known,
# documented Terraform behavior, not specific to this module), so "create it empty, then
# ignore changes" still ends up holding the real secret in state the moment anyone sets it.
#
# A write-only argument is different in kind, not just in policy: Terraform is never allowed
# to persist it to state at all, so there is nothing for a later refresh to read back. This
# was verified directly, not just assumed from the documentation: applying this resource,
# manually overwriting the real value in AWS outside Terraform (aws ssm put-parameter
# --overwrite, exactly what an operator does to set their real secret), then running plan/apply
# again with value_wo_version unchanged produces "No changes" -- Terraform never re-pushes the
# placeholder and never reads the real value back into state either.
#
# value_wo_version is hardcoded to 1, deliberately never bumped: bumping it is what tells
# Terraform to push a new value_wo value, and there's no scenario here where that should ever
# happen automatically -- the whole point is that after this initial placeholder, only the
# operator (via aws ssm put-parameter, outside Terraform) ever changes the real value again.
#
# Each resource is only created when read_slack_secrets_from_ssm is true, and the operator can
# still disable pre-creation entirely by leaving that flag off, per the README.
resource "aws_ssm_parameter" "requester_slack_bot_token" {
  count            = var.read_slack_secrets_from_ssm ? 1 : 0
  name             = var.requester_slack_bot_token_ssm_parameter_name
  type             = "SecureString"
  value_wo         = "REPLACE_ME"
  value_wo_version = 1
  tags             = var.tags

  # This resource holds the real secret's location, not the placeholder, the moment an
  # operator runs `aws ssm put-parameter --overwrite` per the README. Without this,
  # disabling read_slack_secrets_from_ssm (or renaming this parameter's variable) would make
  # Terraform destroy the live parameter -- deleting the real secret, not just a Terraform
  # record of it -- and silently recreate an empty placeholder if re-enabled. Forcing that
  # to be an explicit `terraform state rm` / manual removal instead of a silent side effect
  # of flipping a flag is the whole point of this lifecycle block.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ssm_parameter" "requester_slack_signing_secret" {
  count            = var.read_slack_secrets_from_ssm ? 1 : 0
  name             = var.requester_slack_signing_secret_ssm_parameter_name
  type             = "SecureString"
  value_wo         = "REPLACE_ME"
  value_wo_version = 1
  tags             = var.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ssm_parameter" "revoker_slack_bot_token" {
  count            = var.read_slack_secrets_from_ssm ? 1 : 0
  name             = var.revoker_slack_bot_token_ssm_parameter_name
  type             = "SecureString"
  value_wo         = "REPLACE_ME"
  value_wo_version = 1
  tags             = var.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ssm_parameter" "attribute_syncer_slack_bot_token" {
  count            = var.read_slack_secrets_from_ssm ? 1 : 0
  name             = var.attribute_syncer_slack_bot_token_ssm_parameter_name
  type             = "SecureString"
  value_wo         = "REPLACE_ME"
  value_wo_version = 1
  tags             = var.tags

  lifecycle {
    prevent_destroy = true
  }
}

# These locals build each parameter's ARN from its configured *name* rather than referencing
# the resources above via .arn -- the ARN is a deterministic function of account, region, and
# name, so building it locally works whether or not the resources above exist (guarded by the
# same read_slack_secrets_from_ssm flag), and keeps the IAM policies below from ever needing a
# data source lookup on these parameters (a data source has the identical decrypt-on-refresh
# problem a plain resource would).
locals {
  requester_slack_bot_token_ssm_parameter_arn        = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.requester_slack_bot_token_ssm_parameter_name}"
  requester_slack_signing_secret_ssm_parameter_arn   = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.requester_slack_signing_secret_ssm_parameter_name}"
  revoker_slack_bot_token_ssm_parameter_arn          = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.revoker_slack_bot_token_ssm_parameter_name}"
  attribute_syncer_slack_bot_token_ssm_parameter_arn = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.attribute_syncer_slack_bot_token_ssm_parameter_name}"
}
