module "sso_elevator_dependencies" {
  depends_on      = [null_resource.python_version_check]
  source          = "terraform-aws-modules/lambda/aws"
  version         = "4.16.0"
  create_layer    = true
  create_function = false
  layer_name      = "sso_elevator_dependencies"
  description     = "powertools-pydantic/boto3/slack_bolt"

  compatible_runtimes = ["python3.10"]
  build_in_docker     = var.build_in_docker
  runtime             = "python${local.python_version}"
  docker_image        = "lambda/python:${local.python_version}"
  docker_file         = "${path.module}/src/docker/Dockerfile"
  source_path = [{
    pip_requirements = "${path.module}/layer/deploy_requirements.txt"
    path             = "${path.module}/layer"
    patterns         = ["!python/.venv/.*"]
    prefix_in_zip    = "python"
  }]
}
