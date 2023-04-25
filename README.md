[![FivexL](https://releases.fivexl.io/fivexlbannergit.jpg)](https://fivexl.io/)

# Terraform module to temporary assign AWS IAM Identity Center (SSO) Permission sets to a user

## Why this module?

This module allows you to avoid permanently assigned permission sets and achieve the least privilege access to your AWS accounts. It allows you to assign permission sets to a user for a limited time and revoke them automatically after a specified period of time.

- It will also help you to create more simple permission sets for your users. This will allow you to avoid creating complex IAM policies.
- Each account, OU, or AWS Organization could have their own list of approvers 

## More info
- [Permission Set](https://docs.aws.amazon.com/singlesignon/latest/userguide/permissionsetsconcept.html)
- [User and groups](https://docs.aws.amazon.com/singlesignon/latest/userguide/users-groups-provisioning.html)

## Usage
```terraform

data "aws_ssoadmin_instances" "this" {}

# You will have to create /sso-elevator/slack-signing-secret AWS SSM Parameter
# and store Slack app signing secret there, if you have not created app yet then
# you can leave a dummy value there and update it after Slack app is ready
data "aws_ssm_parameter" "sso_elevator_slack_signing_secret" {
  name = "/sso-elevator/slack-signing-secret"
}

# You will have to create /sso-elevator/slack-bot-token AWS SSM Parameter
# and store Slack bot token there, if you have not created app yet then
# you can leave a dummy value there and update it after Slack app is ready
data "aws_ssm_parameter" "sso_elevator_slack_bot_token" {
  name = "/sso-elevator/slack-bot-token"
}

module "aws_sso_elevator" {
  source                           = "github.com/fivexl/terraform-aws-sso-elevator.git"
  aws_sns_topic_subscription_email = "email@gmail.com"

  slack_signing_secret = data.aws_ssm_parameter.sso_elevator_slack_signing_secret.value
  slack_bot_token      = data.aws_ssm_parameter.sso_elevator_slack_bot_token.value
  slack_channel_id     = "***********"
  schedule_expression  = "cron(0 23 * * ? *)" # revoke access schedule expression
  build_in_docker = true
  revoker_post_update_to_slack = true

  sso_instance_arn = one(data.aws_ssoadmin_instances.this.arns)

  # "Resource", "PermissionSet", "Approvers" can be a string or a list of strings
  # "Resource" & "PermissionSet" can be set to "*" to match all

  # Request will be approved automatically if:
  # - "AllowSelfApproval" is set to true, and requester is in "Approvers" list
  # - "ApprovalIsNotRequired" is set to true

  # If there is only one approver, and "AllowSelfApproval" isn't set to true, nobody will be able to approve the request

  config = [
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : "*",
      "Approvers" : "email@gmail.com",
      "AllowSelfApproval" : true,
    },
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : "Billing",
      "Approvers" : "email@gmail.com",
      "AllowSelfApproval" : true,
    },
    {
      "ResourceType" : "Account",
      "Resource" : ["account_id", "account_id"],
      "PermissionSet" : "ReadOnlyPlus",
      "Approvers" : "email@gmail.com",
    },
    {
      "ResourceType" : "Account",
      "Resource" : "*",
      "PermissionSet" : "ReadOnlyPlus",
      "ApprovalIsNotRequired" : true,
    },
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : ["ReadOnlyPlus", "AdministratorAccess"],
      "Approvers" : ["email@gmail.com"], 
      "AllowSelfApproval" : true,
    },
    {

      # No rescuer hath the rescuer.
      # No Lord hath the champion,
      # no mother and no father,
      # only nothingness above.

      "ResourceType" : "Account",
      "Resource" : "*",
      "PermissionSet" : "*",
      "Approvers" : "org_wide_approver@gmail.com",
      "AllowSelfApproval" : true,
    },
  ]
}

output "aws_sso_elevator_lambda_function_url" {
  value = module.aws_sso_elevator.lambda_function_url
}
```

### Slack App creation
1. Go to https://api.slack.com/
2. Click `create an app`
3. Click `From an app manifest`
4. Select workspace, click `next`
5. Choose `yaml` for app manifest format
6. Update lambda url (from output `aws_sso_elevator_lambda_function_url`) to `request_url` field and paste the following into the text box: 
```yaml
display_information:
  name: WS SSO Access Elevator
  description: AWS SSO access elevator
features:
  bot_user:
    display_name: AWS SSO Access Elevator
    always_online: false
  shortcuts:
    - name: access
      type: global
      callback_id: request_for_access
      description: Request access to Permission Set in AWS Account
oauth_config:
  scopes:
    bot:
      - commands
      - chat:write
      - users:read.email
      - users:read
settings:
  interactivity:
    is_enabled: true
    request_url: <LAMBDA URL GOES HERE - CHECK LAMBDA CONFIGURATION IN AWS CONSOLE OR GET IT FORM TERRAFORM OUTPUT> 
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```
7. Check permissions and click `create`
8. Click `install to workspace`
9. Copy `Signing Secret` # for `slack_signing_secret` module input
10. Copy `Bot User OAuth Token` # for `slack_bot_token` module input

<!-- BEGINNING OF PRE-COMMIT-TERRAFORM DOCS HOOK -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | ~> 1.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 4.64 |
| <a name="requirement_external"></a> [external](#requirement\_external) | >= 1.0 |
| <a name="requirement_local"></a> [local](#requirement\_local) | >= 1.0 |
| <a name="requirement_null"></a> [null](#requirement\_null) | >= 2.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_aws"></a> [aws](#provider\_aws) | >= 4.64 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| <a name="module_access_requester_slack_handler"></a> [access\_requester\_slack\_handler](#module\_access\_requester\_slack\_handler) | terraform-aws-modules/lambda/aws | 4.16.0 |
| <a name="module_access_revoker"></a> [access\_revoker](#module\_access\_revoker) | terraform-aws-modules/lambda/aws | 4.16.0 |
| <a name="module_dynamodb_table_requests"></a> [dynamodb\_table\_requests](#module\_dynamodb\_table\_requests) | terraform-aws-modules/dynamodb-table/aws | 1.2.2 |
| <a name="module_powertools_pydantic"></a> [powertools\_pydantic](#module\_powertools\_pydantic) | terraform-aws-modules/lambda/aws | 4.16.0 |
| <a name="module_python_boto3"></a> [python\_boto3](#module\_python\_boto3) | terraform-aws-modules/lambda/aws | 4.16.0 |
| <a name="module_slack_bolt"></a> [slack\_bolt](#module\_slack\_bolt) | terraform-aws-modules/lambda/aws | 4.16.0 |

## Resources

| Name | Type |
|------|------|
| [aws_cloudwatch_event_rule.sso_elevator_check_on_inconsistency](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.sso_elevator_scheduled_revocation](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_target.check_inconsistency](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.sso_elevator_scheduled_revocation](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_iam_role.eventbridge_role](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.eventbridge_policy](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_lambda_permission.eventbridge](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_permission) | resource |
| [aws_scheduler_schedule_group.one_time_schedule_group](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/scheduler_schedule_group) | resource |
| [aws_sns_topic.dlq](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic) | resource |
| [aws_sns_topic_subscription.dlq](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_subscription) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_iam_policy_document.revoker](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.slack_handler](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |
| [aws_ssoadmin_instances.all](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssoadmin_instances) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_aws_sns_topic_subscription_email"></a> [aws\_sns\_topic\_subscription\_email](#input\_aws\_sns\_topic\_subscription\_email) | value for the email address to subscribe to the SNS topic | `string` | n/a | yes |
| <a name="input_build_in_docker"></a> [build\_in\_docker](#input\_build\_in\_docker) | Whether to build the lambda in a docker container or using local python (poetry) | `bool` | `false` | no |
| <a name="input_config"></a> [config](#input\_config) | value for the SSO Elevator config | `any` | n/a | yes |
| <a name="input_log_level"></a> [log\_level](#input\_log\_level) | value for the log level | `string` | `"INFO"` | no |
| <a name="input_requester_lambda_name"></a> [requester\_lambda\_name](#input\_requester\_lambda\_name) | value for the requester lambda name | `string` | `"access-requester"` | no |
| <a name="input_requester_lambda_name_postfix"></a> [requester\_lambda\_name\_postfix](#input\_requester\_lambda\_name\_postfix) | n/a | `string` | `""` | no |
| <a name="input_revoker_lambda_name"></a> [revoker\_lambda\_name](#input\_revoker\_lambda\_name) | value for the revoker lambda name | `string` | `"access-revoker"` | no |
| <a name="input_revoker_lambda_name_postfix"></a> [revoker\_lambda\_name\_postfix](#input\_revoker\_lambda\_name\_postfix) | n/a | `string` | `""` | no |
| <a name="input_revoker_post_update_to_slack"></a> [revoker\_post\_update\_to\_slack](#input\_revoker\_post\_update\_to\_slack) | value for the revoker post update to Slack | `bool` | `false` | no |
| <a name="input_schedule_expression"></a> [schedule\_expression](#input\_schedule\_expression) | value for the schedule expression | `string` | `"cron(0 23 * * ? *)"` | no |
| <a name="input_schedule_expression_for_check_on_inconsistency"></a> [schedule\_expression\_for\_check\_on\_inconsistency](#input\_schedule\_expression\_for\_check\_on\_inconsistency) | value for the schedule expression for checking on inconsistency | `string` | `"rate(2 hours)"` | no |
| <a name="input_slack_bot_token"></a> [slack\_bot\_token](#input\_slack\_bot\_token) | value for the Slack bot token | `string` | n/a | yes |
| <a name="input_slack_channel_id"></a> [slack\_channel\_id](#input\_slack\_channel\_id) | value for the Slack channel ID | `string` | n/a | yes |
| <a name="input_slack_signing_secret"></a> [slack\_signing\_secret](#input\_slack\_signing\_secret) | value for the Slack signing secret | `string` | n/a | yes |
| <a name="input_sso_instance_arn"></a> [sso\_instance\_arn](#input\_sso\_instance\_arn) | value for the SSO instance ARN | `string` | `""` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | A map of tags to assign to resources. | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_lambda_function_url"></a> [lambda\_function\_url](#output\_lambda\_function\_url) | value for the access\_requester lambda function URL |
<!-- END OF PRE-COMMIT-TERRAFORM DOCS HOOK -->
