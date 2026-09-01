output "aws_sso_elevator_lambda_function_url" {
  value = module.aws_sso_elevator.lambda_function_url
}

output "requester_api_endpoint_url_cli" {
  description = "Pass this to `elevator configure --endpoint` (or set as ELEVATOR_ENDPOINT)."
  value       = module.aws_sso_elevator.requester_api_endpoint_url_cli
}

output "requester_api_execution_arn_cli" {
  description = "Use this to build the execute-api:Invoke IAM policy CLI callers need."
  value       = module.aws_sso_elevator.requester_api_execution_arn_cli
}
