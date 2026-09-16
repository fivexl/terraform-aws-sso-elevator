# Same fix, same reasoning, as revoker_ssm_parameters.tf: the attribute-syncer Lambda's Slack
# bot token used to be pushed into its environment variables as plaintext via a Terraform
# variable, which put the real secret in the Terraform state file too. This resource only
# creates the parameter as an empty placeholder -- the real token must be set once, manually
# (AWS console or `aws ssm put-parameter --overwrite`), entirely outside of Terraform.
# lifecycle.ignore_changes keeps every later plan/apply from touching the value again.
resource "aws_ssm_parameter" "attribute_syncer_slack_bot_token" {
  count = var.attribute_sync_enabled ? 1 : 0

  name        = var.attribute_syncer_slack_bot_token_ssm_parameter_name
  description = "SSO Elevator attribute-syncer Lambda: Slack bot token. Terraform only creates this parameter -- set the real value manually after the first apply."
  type        = "SecureString"
  key_id      = var.ssm_parameter_kms_key_id
  value       = "REPLACE_ME"
  tags        = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}
