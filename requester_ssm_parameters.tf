# Same fix, same reasoning, as revoker_ssm_parameters.tf / attribute_syncer_ssm_parameters.tf:
# the access-requester Lambda's Slack bot token and signing secret used to be pushed into its
# environment variables as plaintext via Terraform variables, which put the real secrets in
# the Terraform state file too. These resources only create the parameters as empty
# placeholders -- the real values must be set once, manually (AWS console or
# `aws ssm put-parameter --overwrite`), entirely outside of Terraform. lifecycle.ignore_changes
# keeps every later plan/apply from touching the values again.
#
# Unlike the revoker, this Lambda genuinely needs both secrets: it receives Slack's own
# signed webhook requests (verified against the signing secret) as well as making outbound
# Slack API calls (using the bot token).
resource "aws_ssm_parameter" "requester_slack_bot_token" {
  name        = var.requester_slack_bot_token_ssm_parameter_name
  description = "SSO Elevator access-requester Lambda: Slack bot token. Terraform only creates this parameter -- set the real value manually after the first apply."
  type        = "SecureString"
  key_id      = var.ssm_parameter_kms_key_id
  value       = "REPLACE_ME"
  tags        = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "requester_slack_signing_secret" {
  name        = var.requester_slack_signing_secret_ssm_parameter_name
  description = "SSO Elevator access-requester Lambda: Slack signing secret. Terraform only creates this parameter -- set the real value manually after the first apply."
  type        = "SecureString"
  key_id      = var.ssm_parameter_kms_key_id
  value       = "REPLACE_ME"
  tags        = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}
