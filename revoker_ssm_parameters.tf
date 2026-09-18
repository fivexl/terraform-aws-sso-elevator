# The revoker Lambda's Slack bot token used to be pushed into its environment variables as
# plaintext via a Terraform variable -- which means the real secret ended up in the Terraform
# state file too, not just the Lambda's configuration. The AWS provider's own docs for
# aws_ssm_parameter warn about the identical problem for a SecureString's own value: "The
# unencrypted value ... will be stored in the raw state as plain-text." Moving the value into
# an SSM parameter instead of an environment variable only helps if Terraform itself never
# knows the real value -- so this resource creates the parameter as an empty placeholder only.
# The real token must be set once, manually (AWS console or `aws ssm put-parameter
# --overwrite`), entirely outside of Terraform. lifecycle.ignore_changes keeps every later
# plan/apply from ever touching the value again, in either direction, once that's done.
#
# SLACK_SIGNING_SECRET used to be passed to this Lambda's environment too, but nothing in
# revoker.py ever reads it: the revoker only makes outbound Slack API calls (using the bot
# token below), it never receives Slack's own signed webhook requests the way the
# access-requester Lambda does, so there is no signature here to ever verify. It has been
# removed from perm_revoker_lambda.tf's environment_variables instead of migrated to SSM,
# since migrating a value nothing reads would just move dead configuration around.
resource "aws_ssm_parameter" "revoker_slack_bot_token" {
  name        = var.revoker_slack_bot_token_ssm_parameter_name
  description = "SSO Elevator revoker Lambda: Slack bot token. Terraform only creates this parameter -- set the real value manually after the first apply."
  type        = "SecureString"
  key_id      = var.ssm_parameter_kms_key_id
  value       = "REPLACE_ME"
  tags        = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}
