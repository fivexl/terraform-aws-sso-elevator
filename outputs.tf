output "sso_elevator_bucket_id" {
  description = "The name of the SSO elevator bucket."
  value       = var.s3_name_of_the_existing_bucket == "" ? module.audit_bucket[0].s3_bucket_id : null
}

output "requester_api_endpoint_url" {
  description = "The full URL to invoke the API. For Slack, set it as the Request URL in the app manifest. For Teams / Bot Framework, set it as the bot messaging endpoint where applicable."
  value       = var.create_api_gateway ? local.full_api_url : null
}

output "chat_platform" {
  description = "The configured chat integration: slack or teams."
  value       = var.chat_platform
}

output "lambda_function_url" {
  description = "value for the access_requester lambda function URL"
  value       = var.create_lambda_url ? module.access_requester_slack_handler.lambda_function_url : null
}

output "config_s3_bucket_name" {
  description = "The name of the S3 bucket for storing configuration and cache data."
  value       = module.config_bucket.s3_bucket_id
}

output "config_s3_bucket_arn" {
  description = "The ARN of the S3 bucket for storing configuration and cache data."
  value       = module.config_bucket.s3_bucket_arn
}

output "elevator_requests_table_name" {
  description = "DynamoDB table name holding access request state and ephemeral UI keys."
  value       = aws_dynamodb_table.elevator_requests.name
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
