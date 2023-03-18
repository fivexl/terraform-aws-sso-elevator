data "aws_ssm_parameter" "slack_bot_token" {
  name            = "/${local.name}/slack_bot_token"
  with_decryption = true
}

data "aws_ssm_parameter" "slack_signing_secret" {
  name            = "/${local.name}/slack_signing_secret"
  with_decryption = true
}
