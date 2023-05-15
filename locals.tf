locals {
  # Full python version is used for checking the python version before deployment in check_python_version.tf
  full_python_version   = "3.10.10"
  # Python version is used for building the docker image in slack_handler_lambda.tf/perm_revoker_lambda.tf/layers.tf
  python_version        = join(".", slice(split(".", local.full_python_version), 0, 2))

  revoker_lambda_name   = "${var.revoker_lambda_name}${var.revoker_lambda_name_postfix}"
  revoker_lambda_arn    = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.revoker_lambda_name}"
  requester_lambda_name = "${var.requester_lambda_name}${var.requester_lambda_name_postfix}"
  requester_lambda_arn  = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.requester_lambda_name}"
  s3_bucket_name        = var.name_of_existing_s3_bucket != "" ? var.name_of_existing_s3_bucket : "${var.s3_bucket_for_audit_entry_name}-audit-trail${var.s3_bucket_name_postfix}"
  s3_bucket_arn         = "arn:aws:s3:::${local.s3_bucket_name}"
  sso_instance_arn      = var.sso_instance_arn == "" ? data.aws_ssoadmin_instances.all[0].arns[0] : var.sso_instance_arn
  schedule_group_name   = "sso-elevator-scheduled-revocation${var.schedule_group_name_postfix}"
}
