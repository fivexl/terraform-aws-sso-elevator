output "sso_elevator_bucket_id" {
  description = "The name of the SSO elevator bucket."
  value       = var.s3_name_of_the_existing_bucket == "" ? module.audit_bucket[0].s3_bucket_id : null
}

output "requester_api_endpoint_url" {
  description = "The full URL to invoke the API. Pass this URL into the Slack App manifest as the Request URL."
  value       = try(local.full_api_url, "")
}

output "lambda_function_url" {
  description = "value for the access_requester lambda function URL"
  value       = try(module.access_requester_slack_handler.lambda_function_url, "")
}
