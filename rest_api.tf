# One REST API for both access-requester routes: POST /access-requester (Slack) and, when
# enable_access_requester_cli is true, POST /access-requester-cli (CLI). REST rather than HTTP
# API because only REST APIs support resource policies, request validation and WAF.

# Organizations permissions the principal running Terraform needs: README "CLI tool".
data "aws_organizations_organization" "current" {
  count = var.enable_access_requester_cli ? 1 : 0
}

resource "aws_api_gateway_rest_api" "requester" {
  name        = var.api_gateway_name
  description = "SSO Elevator access-requester: Slack and CLI routes"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = var.tags
}

locals {
  slack_method_arn = "${aws_api_gateway_rest_api.requester.execution_arn}/${local.api_stage_name}/POST${local.api_resource_path}"
  cli_method_arn   = "${aws_api_gateway_rest_api.requester.execution_arn}/${local.api_stage_name}/POST${local.api_resource_path_cli}"

  # Kept in a local so the deployment trigger below hashes the policy as authored: AWS
  # reformats stored policy JSON, so reading it back makes plan and apply disagree.
  requester_api_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      # Slack calls from the internet; the Lambda verifies the Slack signature.
      [{
        Sid       = "SlackRoute"
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = local.slack_method_arn
      }],
      # Any principal in this AWS Organization; API Gateway returns 403 to callers outside it.
      var.enable_access_requester_cli ? [{
        Sid       = "CliRouteOrgOnly"
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = local.cli_method_arn
        Condition = {
          StringEquals = {
            "aws:PrincipalOrgID" = data.aws_organizations_organization.current[0].id
          }
        }
      }] : []
    )
  })
}

resource "aws_api_gateway_rest_api_policy" "requester" {
  rest_api_id = aws_api_gateway_rest_api.requester.id
  policy      = local.requester_api_policy
}

# Slack route. The header validator rejects unsigned POSTs with 400 before they reach (and
# cold-start) the Lambda; it checks presence only, the Lambda verifies the signature.
resource "aws_api_gateway_resource" "slack" {
  rest_api_id = aws_api_gateway_rest_api.requester.id
  parent_id   = aws_api_gateway_rest_api.requester.root_resource_id
  path_part   = trimprefix(local.api_resource_path, "/")
}

resource "aws_api_gateway_request_validator" "slack" {
  rest_api_id                 = aws_api_gateway_rest_api.requester.id
  name                        = "slack-signature-headers"
  validate_request_parameters = true
  validate_request_body       = false
}

resource "aws_api_gateway_method" "slack" {
  rest_api_id          = aws_api_gateway_rest_api.requester.id
  resource_id          = aws_api_gateway_resource.slack.id
  http_method          = "POST"
  authorization        = "NONE"
  request_validator_id = aws_api_gateway_request_validator.slack.id
  request_parameters = {
    "method.request.header.X-Slack-Signature"         = true
    "method.request.header.X-Slack-Request-Timestamp" = true
  }
}

# The matching Lambda permissions are on the alias (slack_handler_lambda.tf).
resource "aws_api_gateway_integration" "slack" {
  rest_api_id             = aws_api_gateway_rest_api.requester.id
  resource_id             = aws_api_gateway_resource.slack.id
  http_method             = aws_api_gateway_method.slack.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = module.access_requester_alias.lambda_alias_invoke_arn
}

# CLI route, signed with the caller's own AWS credentials.
resource "aws_api_gateway_resource" "cli" {
  count       = var.enable_access_requester_cli ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.requester.id
  parent_id   = aws_api_gateway_rest_api.requester.root_resource_id
  path_part   = trimprefix(local.api_resource_path_cli, "/")
}

resource "aws_api_gateway_method" "cli" {
  count         = var.enable_access_requester_cli ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.requester.id
  resource_id   = aws_api_gateway_resource.cli[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "cli" {
  count                   = var.enable_access_requester_cli ? 1 : 0
  rest_api_id             = aws_api_gateway_rest_api.requester.id
  resource_id             = aws_api_gateway_resource.cli[0].id
  http_method             = aws_api_gateway_method.cli[0].http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = module.access_requester_alias.lambda_alias_invoke_arn
}

resource "aws_api_gateway_deployment" "requester" {
  rest_api_id = aws_api_gateway_rest_api.requester.id

  # Hashes configuration, not ids: REST API resource/method/integration ids stay the same when
  # their configuration changes, and a stage only picks up changes from a new deployment.
  triggers = {
    redeployment = sha1(jsonencode([
      local.requester_api_policy,
      aws_api_gateway_resource.slack.path_part,
      aws_api_gateway_method.slack.authorization,
      aws_api_gateway_method.slack.request_parameters,
      aws_api_gateway_method.slack.request_validator_id,
      aws_api_gateway_request_validator.slack.validate_request_parameters,
      aws_api_gateway_integration.slack.type,
      aws_api_gateway_integration.slack.uri,
      aws_api_gateway_resource.cli[*].path_part,
      aws_api_gateway_method.cli[*].authorization,
      aws_api_gateway_integration.cli[*].type,
      aws_api_gateway_integration.cli[*].uri,
    ]))
  }

  # A policy change takes effect only through a new deployment, so deploy after it is attached.
  depends_on = [
    aws_api_gateway_rest_api_policy.requester,
    aws_api_gateway_integration.slack,
    aws_api_gateway_integration.cli,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

# REST API stage access logs need the account-wide aws_api_gateway_account CloudWatch role,
# which this module does not manage: other APIs in the account may rely on its current value.
resource "aws_cloudwatch_log_group" "api_access_logs" {
  count             = var.api_gateway_access_logs_enabled ? 1 : 0
  name              = "/aws/apigateway/${var.api_gateway_name}/${local.api_stage_name}/access"
  retention_in_days = var.logs_retention_in_days
  tags              = var.tags
}

resource "aws_api_gateway_stage" "requester" {
  deployment_id = aws_api_gateway_deployment.requester.id
  rest_api_id   = aws_api_gateway_rest_api.requester.id
  stage_name    = local.api_stage_name
  tags          = var.tags

  dynamic "access_log_settings" {
    for_each = var.api_gateway_access_logs_enabled ? [1] : []
    content {
      destination_arn = aws_cloudwatch_log_group.api_access_logs[0].arn
      format = jsonencode({
        requestId          = "$context.requestId"
        ip                 = "$context.identity.sourceIp"
        requestTime        = "$context.requestTime"
        httpMethod         = "$context.httpMethod"
        resourcePath       = "$context.resourcePath"
        status             = "$context.status"
        responseLength     = "$context.responseLength"
        errorMessage       = "$context.error.message"
        validationError    = "$context.error.validationErrorString"
        integrationStatus  = "$context.integration.status"
        integrationLatency = "$context.integration.latency"
        wafResponseCode    = "$context.wafResponseCode"
      })
    }
  }
}

# REST APIs throttle per method through a separate resource; without it a method gets only the
# account-wide default quota.
resource "aws_api_gateway_method_settings" "slack" {
  rest_api_id = aws_api_gateway_rest_api.requester.id
  stage_name  = aws_api_gateway_stage.requester.stage_name
  method_path = "${aws_api_gateway_resource.slack.path_part}/POST"

  settings {
    throttling_burst_limit = var.api_gateway_throttling_burst_limit
    throttling_rate_limit  = var.api_gateway_throttling_rate_limit
  }
}

resource "aws_api_gateway_method_settings" "cli" {
  count       = var.enable_access_requester_cli ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.requester.id
  stage_name  = aws_api_gateway_stage.requester.stage_name
  method_path = "${aws_api_gateway_resource.cli[0].path_part}/POST"

  settings {
    throttling_burst_limit = var.api_gateway_throttling_burst_limit
    throttling_rate_limit  = var.api_gateway_throttling_rate_limit
  }
}
