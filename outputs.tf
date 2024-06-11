output "lambda_function_url" {
  description = "value for the access_requester lambda function URL"
  value       = module.access_requester_slack_handler.lambda_function_url
}

output "sso_elevator_bucket_id" {
  description = "The name of the SSO elevator bucket."
  value       = var.s3_name_of_the_existing_bucket == "" ? module.audit_bucket[0].s3_bucket_id : null
}
