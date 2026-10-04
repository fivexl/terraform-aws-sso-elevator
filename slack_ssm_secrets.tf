# Terraform creates each parameter with a placeholder; operators set the real value with
# `aws ssm put-parameter --overwrite`. value_wo is never persisted to state, and with
# value_wo_version left at 1 Terraform never writes the parameter again after creation.
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
