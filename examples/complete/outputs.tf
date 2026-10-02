output "requester_api_endpoint_url_cli" {
  description = "Pass this to `elevator configure --endpoint` (or set as ELEVATOR_ENDPOINT)."
  value       = module.aws_sso_elevator.requester_api_endpoint_url_cli
}
