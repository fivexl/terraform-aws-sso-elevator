output "requester_api_endpoint_url_cli" {
  description = "Pass this to `elevator configure --endpoint` (or set as ELEVATOR_ENDPOINT)."
  value       = module.aws_sso_elevator.requester_api_endpoint_url_cli
}

output "requester_api_execution_arn_cli" {
  description = "Grant execute-api:Invoke on this ARN in the permission sets of CLI callers in other accounts."
  value       = module.aws_sso_elevator.requester_api_execution_arn_cli
}

output "requester_api_id" {
  description = "Pass this to `elevator configure --api-id` when the CLI reaches the API through a custom domain."
  value       = module.aws_sso_elevator.requester_api_id
}
