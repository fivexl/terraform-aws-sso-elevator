# Secrets Manager secret for SSO Elevator configuration
# Only created when use_secrets_manager_for_config is true
resource "aws_secretsmanager_secret" "sso_elevator_config" {
  count = var.use_secrets_manager_for_config ? 1 : 0

  name_prefix             = "${var.requester_lambda_name}-config-"
  description             = "SSO Elevator statements configuration"
  recovery_window_in_days = 7

  tags = var.tags
}

# Secret version containing the actual configuration statements
resource "aws_secretsmanager_secret_version" "sso_elevator_config" {
  count = var.use_secrets_manager_for_config ? 1 : 0

  secret_id     = aws_secretsmanager_secret.sso_elevator_config[0].id
  secret_string = jsonencode(var.config)
}

# Secrets Manager secret for SSO Elevator group configuration
# Only created when use_secrets_manager_for_config is true
resource "aws_secretsmanager_secret" "sso_elevator_group_config" {
  count = var.use_secrets_manager_for_config ? 1 : 0

  name_prefix             = "${var.requester_lambda_name}-group-config-"
  description             = "SSO Elevator group statements configuration"
  recovery_window_in_days = 7

  tags = var.tags
}

# Secret version containing the actual group configuration statements
resource "aws_secretsmanager_secret_version" "sso_elevator_group_config" {
  count = var.use_secrets_manager_for_config ? 1 : 0

  secret_id     = aws_secretsmanager_secret.sso_elevator_group_config[0].id
  secret_string = jsonencode(var.group_config)
}
