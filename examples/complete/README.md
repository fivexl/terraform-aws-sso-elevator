# Complete Example

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | ~> 1.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 6.28 |
| <a name="requirement_external"></a> [external](#requirement\_external) | >= 1.0 |
| <a name="requirement_local"></a> [local](#requirement\_local) | >= 1.0 |
| <a name="requirement_null"></a> [null](#requirement\_null) | >= 2.0 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | >= 6.28 |

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_aws_sso_elevator"></a> [aws\_sso\_elevator](#module\_aws\_sso\_elevator) | ../.. | n/a |

## Resources

| Name | Type |
| ---- | ---- |
| [aws_ssoadmin_instances.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssoadmin_instances) | data source |

## Inputs

No inputs.

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_requester_api_endpoint_url"></a> [requester\_api\_endpoint\_url](#output\_requester\_api\_endpoint\_url) | The URL to invoke the Lambda function |
| <a name="output_requester_api_endpoint_url_cli"></a> [requester\_api\_endpoint\_url\_cli](#output\_requester\_api\_endpoint\_url\_cli) | Pass this to `elevator configure --endpoint` (or set as ELEVATOR\_ENDPOINT). |
| <a name="output_requester_api_execution_arn_cli"></a> [requester\_api\_execution\_arn\_cli](#output\_requester\_api\_execution\_arn\_cli) | Grant execute-api:Invoke on this ARN in the permission sets of CLI callers in other accounts. |
<!-- END_TF_DOCS -->