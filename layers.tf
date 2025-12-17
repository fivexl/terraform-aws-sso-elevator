module "sso_elevator_dependencies" {
  count           = var.use_pre_created_image ? 0 : 1
  source          = "terraform-aws-modules/lambda/aws"
  version         = "8.1.2"
  create_layer    = true
  create_function = false
  layer_name      = "sso_elevator_dependencies"
  description     = "powertools-pydantic/boto3/slack_bolt"

  compatible_runtimes      = ["python3.13"]
  compatible_architectures = [var.lambda_architecture]
  build_in_docker          = true
  runtime                  = "python${local.python_version}"
  source_path = [{
    path             = "${path.module}/layer"
    pip_requirements = "${path.module}/layer/requirements.txt"
    prefix_in_zip    = "python"
  }]
}
