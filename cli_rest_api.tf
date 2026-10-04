# CLI access-request path: a REST API separate from module.http_api (slack_handler_lambda.tf),
# because only REST APIs support resource policies. The policy admits any principal in this
# AWS Organization (aws:PrincipalOrgID); API Gateway returns 403 to callers outside it.

# Organizations permissions the principal running Terraform needs: README "CLI tool".
data "aws_organizations_organization" "current" {
  count = local.create_cli_rest_api ? 1 : 0
}

resource "aws_api_gateway_rest_api" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  name        = "${var.api_gateway_name}-cli"
  description = "SSO Elevator CLI access-request route"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = var.tags
}

locals {
  # Kept in a local so the deployment trigger below hashes the policy as authored: AWS
  # reformats stored policy JSON, so reading it back makes plan and apply disagree.
  cli_rest_api_policy = local.create_cli_rest_api ? jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "execute-api:Invoke"
      # The full ARN, as AWS stores it: the "execute-api:/*" shorthand is expanded on save
      # and shows as a change on every plan. It needs the API id, hence a separate policy resource.
      Resource = "${aws_api_gateway_rest_api.cli[0].execution_arn}/*"
      Condition = {
        StringEquals = {
          "aws:PrincipalOrgID" = data.aws_organizations_organization.current[0].id
        }
      }
    }]
  }) : null
}

resource "aws_api_gateway_rest_api_policy" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.cli[0].id
  policy      = local.cli_rest_api_policy
}

resource "aws_api_gateway_resource" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.cli[0].id
  parent_id   = aws_api_gateway_rest_api.cli[0].root_resource_id
  path_part   = trimprefix(local.api_resource_path_cli, "/")
}

resource "aws_api_gateway_method" "cli" {
  count         = local.create_cli_rest_api ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.cli[0].id
  resource_id   = aws_api_gateway_resource.cli[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

# The matching Lambda permission is in the Lambda module's allowed_triggers (slack_handler_lambda.tf).
resource "aws_api_gateway_integration" "cli" {
  count                   = local.create_cli_rest_api ? 1 : 0
  rest_api_id             = aws_api_gateway_rest_api.cli[0].id
  resource_id             = aws_api_gateway_resource.cli[0].id
  http_method             = aws_api_gateway_method.cli[0].http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = module.access_requester_slack_handler.lambda_function_invoke_arn
}

resource "aws_api_gateway_deployment" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.cli[0].id

  # Hashes configuration, not ids: REST API resource/method/integration ids stay the same when
  # their configuration changes, and a stage only picks up changes from a new deployment.
  triggers = {
    redeployment = sha1(jsonencode([
      local.cli_rest_api_policy,
      aws_api_gateway_resource.cli[0].path_part,
      aws_api_gateway_method.cli[0].http_method,
      aws_api_gateway_method.cli[0].authorization,
      aws_api_gateway_integration.cli[0].type,
      aws_api_gateway_integration.cli[0].integration_http_method,
      aws_api_gateway_integration.cli[0].uri,
    ]))
  }

  # A policy change takes effect only through a new deployment, so deploy after it is attached.
  depends_on = [aws_api_gateway_rest_api_policy.cli]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "cli" {
  count         = local.create_cli_rest_api ? 1 : 0
  deployment_id = aws_api_gateway_deployment.cli[0].id
  rest_api_id   = aws_api_gateway_rest_api.cli[0].id
  stage_name    = local.api_stage_name
  tags          = var.tags

  # No access logging: REST API stage logs need the account-wide aws_api_gateway_account
  # CloudWatch role, which this module does not manage and could conflict with other owners.
}

# REST APIs throttle per method through a separate resource; without it the method gets only
# the account-wide default quota. Same limits as the Slack route.
resource "aws_api_gateway_method_settings" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.cli[0].id
  stage_name  = aws_api_gateway_stage.cli[0].stage_name
  method_path = "${aws_api_gateway_resource.cli[0].path_part}/POST"

  settings {
    throttling_burst_limit = var.api_gateway_throttling_burst_limit
    throttling_rate_limit  = var.api_gateway_throttling_rate_limit
  }
}
