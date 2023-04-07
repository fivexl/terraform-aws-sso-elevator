locals {
  revoker_lambda_name   = "${var.revoker_lambda_name}${var.revoker_lambda_name_postfix}"
  requester_lambda_name = "${var.requester_lambda_name}${var.requester_lambda_name_postfix}"
  sso_instance_arn      = var.sso_instance_arn == "" ? data.aws_ssoadmin_instances.all[0].arns[0] : var.sso_instance_arn
}
