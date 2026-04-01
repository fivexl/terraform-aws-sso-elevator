output "requester_api_endpoint_url" {
  description = "The URL to invoke the Lambda function"
  value       = module.aws_sso_elevator.requester_api_endpoint_url
}
