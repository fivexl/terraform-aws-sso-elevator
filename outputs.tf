output "sso_elevator_bucket_id" {
  description = "The name of the SSO elevator bucket."
  value       = var.s3_name_of_the_existing_bucket == "" ? module.audit_bucket[0].s3_bucket_id : null
}

output "requester_api_endpoint_url" {
  description = "The full URL to invoke the API. Pass this URL into the Slack App manifest as the Request URL."
  value       = "${aws_api_gateway_stage.requester.invoke_url}${local.api_resource_path}"
}

output "requester_api_endpoint_url_cli" {
  description = "The full URL for the CLI's access-request route. Pass this to `elevator configure --endpoint` (or set as ELEVATOR_ENDPOINT). null when enable_access_requester_cli is false."
  value       = var.enable_access_requester_cli ? "${aws_api_gateway_stage.requester.invoke_url}${local.api_resource_path_cli}" : null
}

output "requester_api_execution_arn_cli" {
  description = "The execute-api ARN of the CLI's access-request route. Callers in other accounts need execute-api:Invoke on this ARN in their permission set. null when enable_access_requester_cli is false."
  value       = var.enable_access_requester_cli ? local.cli_method_arn : null
}

output "requester_api_id" {
  description = "The id of the requester REST API. CLI users behind a custom domain pass it to `elevator configure --api-id`."
  value       = aws_api_gateway_rest_api.requester.id
}

output "waf_web_acl_arn" {
  description = "ARN of the web ACL associated with the requester API: the module-created one (waf_enabled) or waf_web_acl_arn. null when neither is set."
  value       = local.waf_web_acl_arn
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
