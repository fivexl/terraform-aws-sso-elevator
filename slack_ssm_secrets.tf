# Slack secrets in SSM Parameter Store -- see read_slack_secrets_from_ssm in vars.tf.
#
# Deliberately no aws_ssm_parameter resources here: Terraform never creates, reads, or
# manages these parameters at all. An `aws_ssm_parameter` resource still has its value read
# back into state on every future refresh even with lifecycle.ignore_changes on `value` (the
# AWS provider calls GetParameter with decryption to refresh state regardless of that
# lifecycle rule -- a known, documented Terraform behavior, not specific to this module), so
# a "create it empty, then ignore changes" resource still ends up holding the real secret in
# state the moment anyone actually sets it. The only way to guarantee Terraform never learns
# the real value is for Terraform to never manage the resource holding it.
#
# So when read_slack_secrets_from_ssm is enabled, each parameter must already exist and be
# populated by the time Terraform applies -- created and set entirely outside of Terraform
# (console or `aws ssm put-parameter`), per the README. These locals only build the ARNs
# those parameters *would* have, from their configured names, so the IAM policies below can
# grant exactly scoped access without a data source ever looking the resources up (a data
# source has the identical decrypt-on-refresh problem as a resource would).
locals {
  requester_slack_bot_token_ssm_parameter_arn        = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.requester_slack_bot_token_ssm_parameter_name}"
  requester_slack_signing_secret_ssm_parameter_arn   = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.requester_slack_signing_secret_ssm_parameter_name}"
  revoker_slack_bot_token_ssm_parameter_arn          = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.revoker_slack_bot_token_ssm_parameter_name}"
  attribute_syncer_slack_bot_token_ssm_parameter_arn = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${var.attribute_syncer_slack_bot_token_ssm_parameter_name}"
}
