output "lambda_function_url" {
  description = "value for the access_requester lambda function URL"
  value       = module.access_requester_slack_handler.lambda_function_url
}
