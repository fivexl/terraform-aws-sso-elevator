# ==========================================
# CLI access-request path: REST API (issue #214)
# ==========================================
#
# A separate API Gateway for the CLI's signed requests only -- distinct from module.http_api in
# slack_handler_lambda.tf, which now serves the Slack route exclusively (this REST API replaced
# that module's own former CLI route; see git history for #214 if you need the old HTTP API CLI
# route's shape). REST API, not HTTP API, is required here because only REST API supports
# resource policies: the mechanism that lets a request from any account in the AWS Organization
# reach this endpoint with zero per-account IAM setup, gated by aws:PrincipalOrgID instead of a
# single hardcoded expected account id. HTTP API has no equivalent. Confirmed empirically against
# a real AWS Organization before this was written: a same-org caller in a *different* account was
# let through to the Lambda with no extra setup, and a caller genuinely outside the org was
# rejected by API Gateway itself (403) before the Lambda ever ran, then confirmed again against
# this exact Terraform (not just a spike) with the real application code deployed.
#
# Past this resource policy, cli_auth.py's own identity check no longer re-verifies account
# membership at all -- that check was retired (see cli_auth.py's module docstring for the
# reasoning and the accepted residual risk) once this resource policy was confirmed to be doing
# that job at the API Gateway layer. A different-account, same-org caller now succeeds end to
# end, not just reaches the Lambda -- confirmed live and by
# test_handle_cli_access_request_accepts_caller_from_a_different_account.
#
# Every resource here is gated on local.create_cli_rest_api (see locals.tf), which defaults to
# false so upgrading an existing deployment doesn't silently add any new
# AWS_IAM-authorized entry point.

# Requires organizations:DescribeOrganization on whichever principal runs `terraform apply` --
# same permission the resource policy below needs to build its aws:PrincipalOrgID condition
# from a real value. Only requested when the CLI route is actually being created, same as
# every other CLI-only resource in this file.
data "aws_organizations_organization" "current" {
  count = local.create_cli_rest_api ? 1 : 0
}

locals {
  # "execute-api:/*" is API Gateway's own shorthand for "any stage/method/resource of this
  # same REST API" inside a resource policy attached to that API -- confirmed working in all
  # three live test scenarios (same account, different account in the same org, outside the
  # org entirely), so kept exactly as validated rather than spelled out as a full ARN.
  #
  # Extracted into this local, rather than read back off
  # aws_api_gateway_rest_api.cli[0].policy, specifically so aws_api_gateway_deployment's
  # trigger hash below can use the value as originally authored: AWS reformats a resource
  # policy's JSON slightly when it stores it (key ordering/whitespace), so reading it back
  # from the resource produces a different string at apply time than Terraform computed at
  # plan time for the exact same policy -- which Terraform reports as "Provider produced
  # inconsistent final plan". Found and fixed the same way while building the spike that
  # proved this design out.
  cli_rest_api_policy = local.create_cli_rest_api ? jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "execute-api:Invoke"
      Resource  = "execute-api:/*"
      Condition = {
        StringEquals = {
          "aws:PrincipalOrgID" = data.aws_organizations_organization.current[0].id
        }
      }
    }]
  }) : null
}

resource "aws_api_gateway_rest_api" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  name        = "${var.api_gateway_name}-cli"
  description = "REST API for SSO Elevator's access-requester Lambda, CLI route only -- see issue #214"
  policy      = local.cli_rest_api_policy

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = var.tags
}

resource "aws_api_gateway_resource" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.cli[0].id
  parent_id   = aws_api_gateway_rest_api.cli[0].root_resource_id
  # Reuses the shared local (locals.tf's api_resource_path_cli), not a separately hardcoded copy
  # of the literal -- it's the same path string main.py's CLI_ACCESS_REQUEST_PATH expects.
  path_part = trimprefix(local.api_resource_path_cli, "/")
}

resource "aws_api_gateway_method" "cli" {
  count         = local.create_cli_rest_api ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.cli[0].id
  resource_id   = aws_api_gateway_resource.cli[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "cli" {
  count                   = local.create_cli_rest_api ? 1 : 0
  rest_api_id             = aws_api_gateway_rest_api.cli[0].id
  resource_id             = aws_api_gateway_resource.cli[0].id
  http_method             = aws_api_gateway_method.cli[0].http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = module.access_requester_slack_handler.lambda_function_invoke_arn
}

# The permission itself lives on the Lambda module's own allowed_triggers map
# (slack_handler_lambda.tf), alongside the two existing permissions on this same Lambda, rather
# than as a standalone aws_lambda_permission resource here -- one mechanism for "who may invoke
# this Lambda", not two.

resource "aws_api_gateway_deployment" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.cli[0].id

  # Hashes the resources' actual configuration, not their .id attributes (found in review): a
  # REST API resource/method/integration's own `id` is a stable composite key
  # (rest-api-id/resource-id/http-method-shaped) that does NOT change when the resource's real
  # configuration does -- so hashing those ids, as the throwaway spike that proved this design
  # out did, would silently fail to trigger a redeployment if e.g. the integration's uri ever
  # changed (var.requester_lambda_name being customized) or the method's authorization changed.
  # AWS does not propagate such changes to an already-deployed stage without a new deployment.
  # local.cli_rest_api_policy, not aws_api_gateway_rest_api.cli[0].policy -- see that local's
  # own comment for why.
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

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_api_gateway_integration.cli]
}

resource "aws_api_gateway_stage" "cli" {
  count         = local.create_cli_rest_api ? 1 : 0
  deployment_id = aws_api_gateway_deployment.cli[0].id
  rest_api_id   = aws_api_gateway_rest_api.cli[0].id
  # Reuses the same stage name the HTTP API's own stage uses (locals.tf's api_stage_name), not
  # a separately hardcoded copy of the literal.
  stage_name = local.api_stage_name
  tags       = var.tags

  # No access_log_settings here, unlike module.http_api's stage (slack_handler_lambda.tf) --
  # deliberate, not an oversight: REST API stage access logging additionally requires an
  # account-level CloudWatch role (aws_api_gateway_account), a single global, per-account/region
  # setting this module has never managed before. Introducing it here would make this module
  # start managing that account-wide setting for the first time, risking a conflict with
  # whatever else in the same AWS account already configures it (or expects to configure it
  # later) -- too broad a side effect to take on as part of this change. Revisit if/when this
  # module takes on managing that account-wide setting for some other reason.
}

# Without this, the method falls back to API Gateway's shared, account-wide default REST API
# quota instead of a per-route limit (found in review) -- unlike the Slack route
# (module.http_api, which sets throttling_burst_limit/throttling_rate_limit inline per route),
# a REST API's per-method throttle is a separate resource. Reuses the same two variables the
# Slack route already uses, rather than inventing CLI-specific ones: this route's identity
# verification is more expensive per request (a full paginated Identity Store scan on a cache
# miss), not less, so it has at least as much reason to be throttled.
resource "aws_api_gateway_method_settings" "cli" {
  count       = local.create_cli_rest_api ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.cli[0].id
  stage_name  = aws_api_gateway_stage.cli[0].stage_name
  method_path = "${trimprefix(local.api_resource_path_cli, "/")}/POST"

  settings {
    throttling_burst_limit = var.api_gateway_throttling_burst_limit
    throttling_rate_limit  = var.api_gateway_throttling_rate_limit
  }
}
