
module "powertools_pydantic" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.10.1"

  create_layer  = true
  create_function = false
  layer_name    = "powertools-pydantic"
  description   = "AWS Lambda Powertools with Pydantic"

  compatible_runtimes = ["python3.9"]
  runtime         = "python3.9"
  source_path = [{
      poetry_install  = true
      path            = "${path.module}/layers/powertools-pydantic"
      patterns        = ["!python/.venv/.*"]
      prefix_in_zip   = "python"
    }]
}

module "slack_bolt" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "4.10.1"

  create_layer  = true
  create_function = false
  layer_name    = "python-slack-bolt"
  description   = "A framework that makes Slack app development fast and straight-forward."

  compatible_runtimes = ["python3.9"]
  runtime         = "python3.9"
  source_path = [{
      poetry_install  = true
      path            = "${path.module}/layers/python-slack-bolt"
      patterns        = ["!python/.venv/.*"]
      prefix_in_zip   = "python"
    }]
}