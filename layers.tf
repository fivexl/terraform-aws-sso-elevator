module "powertools_pydantic" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.16.0"

  create_layer    = true
  create_function = false
  layer_name      = "powertools-pydantic"
  description     = "AWS Lambda Powertools with Pydantic"

  compatible_runtimes = ["python3.10"]
  build_in_docker     = var.build_in_docker
  runtime             = "python3.10"
  docker_image        = "build-python3.10-poetry"
  docker_file         = "${path.module}/src/docker/Dockerfile"
  source_path = [{
    poetry_install = true
    path           = "${path.module}/layers/powertools-pydantic"
    patterns       = ["!python/.venv/.*"]
    prefix_in_zip  = "python"
  }]
}

module "slack_bolt" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.16.0"

  create_layer    = true
  create_function = false
  layer_name      = "python-slack-bolt"
  description     = "A framework that makes Slack app development fast and straight-forward."

  compatible_runtimes = ["python3.10"]
  build_in_docker     = var.build_in_docker
  runtime             = "python3.10"
  docker_image        = "build-python3.10-poetry"
  docker_file         = "${path.module}/src/docker/Dockerfile"
  source_path = [{
    poetry_install = true
    path           = "${path.module}/layers/python-slack-bolt"
    patterns       = ["!python/.venv/.*"]
    prefix_in_zip  = "python"
  }]
}

module "python_boto3" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.16.0"

  create_layer    = true
  create_function = false
  layer_name      = "python-boto3"
  description     = "Python Boto3."

  compatible_runtimes = ["python3.10"]
  build_in_docker     = var.build_in_docker
  runtime             = "python3.10"
  docker_image        = "build-python3.10-poetry"
  docker_file         = "${path.module}/src/docker/Dockerfile"
  source_path = [{
    poetry_install = true
    path           = "${path.module}/layers/python-boto3"
    patterns       = ["!python/.venv/.*"]
    prefix_in_zip  = "python"
  }]
}
