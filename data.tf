data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

data "aws_ssoadmin_instances" "all" {
  count = var.sso_instance_arn == "" ? 1 : 0
}
