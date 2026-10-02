output "sso_elevator_bucket_id" {
  description = "The name of the SSO elevator bucket."
  value       = var.s3_name_of_the_existing_bucket == "" ? module.audit_bucket[0].s3_bucket_id : null
}

output "requester_api_endpoint_url" {
  description = "The full URL to invoke the API. Pass this URL into the Slack App manifest as the Request URL."
  value       = var.create_api_gateway ? local.full_api_url : null
}

# The CLI's access-request route (see cli_rest_api.tf, issue #214) -- a REST API, not the
# HTTP API above (that one is Slack-only now). Unlike an HTTP API, no per-caller
# execute-api:Invoke IAM policy needs building for this: the REST API's own resource policy
# already lets any account in this AWS Organization reach API Gateway, so there's no
# "_execution_arn_cli" output to pair with this one (there was, for the removed HTTP API CLI
# route). See the README's CLI section and src/cli_auth.py's module docstring for exactly what
# identity verification a caller from a different account still goes through past this point.
output "requester_api_endpoint_url_cli" {
  description = "The full URL for the CLI's access-request route. Pass this to `elevator configure --endpoint` (or set as ELEVATOR_ENDPOINT). null unless enable_access_requester_cli is also true."
  value       = local.create_cli_rest_api ? "${aws_api_gateway_stage.cli[0].invoke_url}${local.api_resource_path_cli}" : null
}

output "config_s3_bucket_name" {
  description = "The name of the S3 bucket for storing configuration and cache data."
  value       = module.config_bucket.s3_bucket_id
}

output "config_s3_bucket_arn" {
  description = "The ARN of the S3 bucket for storing configuration and cache data."
  value       = module.config_bucket.s3_bucket_arn
}


# Attribute Syncer Outputs
output "attribute_syncer_lambda_arn" {
  description = "The ARN of the attribute syncer Lambda function."
  value       = var.attribute_sync_enabled ? module.attribute_syncer[0].lambda_function_arn : null
}

output "attribute_syncer_lambda_name" {
  description = "The name of the attribute syncer Lambda function."
  value       = var.attribute_sync_enabled ? module.attribute_syncer[0].lambda_function_name : null
}

output "attribute_sync_schedule_rule_arn" {
  description = "The ARN of the EventBridge rule that triggers the attribute syncer."
  value       = var.attribute_sync_enabled ? aws_cloudwatch_event_rule.attribute_sync_schedule[0].arn : null
}
