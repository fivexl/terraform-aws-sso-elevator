module "sso_elevator_dependencies" {
  count           = var.use_pre_created_image ? 0 : 1
  source          = "terraform-aws-modules/lambda/aws"
  version         = "7.19.0"
  create_layer    = true
  create_function = false
  layer_name      = "sso_elevator_dependencies"
  description     = "powertools-pydantic/boto3/slack_bolt"

  compatible_runtimes = ["python3.13"]
  build_in_docker     = true
  runtime             = "python${local.python_version}"
  docker_image        = "lambda/python:${local.python_version}"
  docker_file         = "${path.module}/src/docker/Dockerfile.layer"
  source_path = [{
    path          = "${path.module}/layer"
    patterns      = ["!python/.venv/.*"]
    prefix_in_zip = "python"
  }]
}
