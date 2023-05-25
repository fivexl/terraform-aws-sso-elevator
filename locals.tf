locals {
  # Full python version is used for checking the python version before deployment in check_python_version.tf
  full_python_version = "3.10.10"
  # Python version is used for building the docker image in slack_handler_lambda.tf/perm_revoker_lambda.tf/layers.tf
  python_version = join(".", slice(split(".", local.full_python_version), 0, 2))

  revoker_lambda_arn    = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.revoker_lambda_name}"
  requester_lambda_arn  = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.requester_lambda_name}"
  sso_instance_arn      = var.sso_instance_arn == "" ? data.aws_ssoadmin_instances.all[0].arns[0] : var.sso_instance_arn

  # In case of default value for var.s3_bucket_name_for_audit_entry, we append a random string to the bucket name to make it unique.
  # In case of non-default value for var.s3_bucket_name_for_audit_entry, we use the value as is and expect the name is unique.
  # In case of var.s3_name_of_the_existing_bucket, we skip creating a new bucket and use the existing one.
  s3_bucket_name_for_audit_entry = var.s3_bucket_name_for_audit_entry != "sso-elevator-audit-entry" ? var.s3_bucket_name_for_audit_entry : "sso-elevator-audit-entry-${random_string.random.result}"
  s3_bucket_name                 = var.s3_name_of_the_existing_bucket != "" ? var.s3_name_of_the_existing_bucket : local.s3_bucket_name_for_audit_entry
  s3_bucket_arn                  = "arn:aws:s3:::${local.s3_bucket_name}"
}

resource "random_string" "random" {
  length  = 16
  special = false
  upper   = false
  numeric = false
}
