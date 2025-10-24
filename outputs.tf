output "sso_elevator_bucket_id" {
  description = "The name of the SSO elevator bucket."
  value       = var.s3_name_of_the_existing_bucket == "" ? module.audit_bucket[0].s3_bucket_id : null
}

output "requester_api_endpoint_url" {
  description = "The full URL to invoke the API. Pass this URL into the Slack App manifest as the Request URL."
  value       = var.create_api_gateway ? local.full_api_url : null
}

output "lambda_function_url" {
  description = "value for the access_requester lambda function URL"
  value       = var.create_lambda_url ? module.access_requester_slack_handler.lambda_function_url : null
}

output "cache_dynamodb_table_name" {
  description = "The name of the DynamoDB table for caching AWS accounts and permission sets."
  value       = var.cache_ttl_minutes > 0 ? aws_dynamodb_table.sso_elevator_cache[0].name : null
}

output "cache_dynamodb_table_arn" {
  description = "The ARN of the DynamoDB table for caching AWS accounts and permission sets."
  value       = var.cache_ttl_minutes > 0 ? aws_dynamodb_table.sso_elevator_cache[0].arn : null
}
