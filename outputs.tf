output "sso_elevator_bucket_id" {
  description = "The name of the SSO elevator bucket."
  value       = var.s3_name_of_the_existing_bucket == "" ? module.audit_bucket[0].s3_bucket_id : null
}

output "requester_api_endpoint_url" {
  description = "The full URL to invoke the API. For Slack, set it as the Request URL in the app manifest. For Teams / Bot Framework, set it as the bot messaging endpoint where applicable. When a custom domain is configured, this is the custom-domain URL (the default execute-api endpoint is disabled)."
  value = !var.create_api_gateway ? null : (
    var.api_gateway_custom_domain_name != "" ? "https://${var.api_gateway_custom_domain_name}${local.api_resource_path}" : local.full_api_url
  )
}

output "api_gateway_domain_name_target" {
  description = "Target domain name of the API Gateway custom domain (for a Route53 alias record). Null when no custom domain is configured."
  value       = var.create_api_gateway && var.api_gateway_custom_domain_name != "" ? module.http_api[0].domain_name_target_domain_name : null
}

output "api_gateway_domain_name_hosted_zone_id" {
  description = "Hosted zone ID of the API Gateway custom domain (for a Route53 alias record). Null when no custom domain is configured."
  value       = var.create_api_gateway && var.api_gateway_custom_domain_name != "" ? module.http_api[0].domain_name_hosted_zone_id : null
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
