data "aws_ssoadmin_instances" "all" {
  count = var.sso_instance_arn == "" ? 1 : 0
}

locals {
  # tflint-ignore: terraform_unused_declarations
  invalid_ssoadmin_instance_configuration = (length(data.aws_ssoadmin_instances.all) != 1) ? tobool("There is multiple sso instances found. Please specify one.") : true
}
