provider "aws" {
  region = "eu-central-1"

  # Make it faster by skipping something
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_credentials_validation = true
  skip_requesting_account_id  = true
}


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
  source                           = "../.."
  aws_sns_topic_subscription_email = "email@gmail.com"

  slack_signing_secret                           = data.aws_ssm_parameter.sso_elevator_slack_signing_secret.value
  slack_bot_token                                = data.aws_ssm_parameter.sso_elevator_slack_bot_token.value
  slack_channel_id                               = "***********"
  schedule_expression                            = "cron(0 23 * * ? *)" # revoke access schedule expression
  schedule_expression_for_check_on_inconsistency = "rate(1 hour)"
  revoker_post_update_to_slack                   = true
  send_dm_if_user_not_in_channel                 = true

  sso_instance_arn = one(data.aws_ssoadmin_instances.this.arns)

  approver_renotification_initial_wait_time  = 15
  approver_renotification_backoff_multiplier = 2

  # S3 config bucket configuration (caching is enabled by default)
  # config_bucket_name     = "sso-elevator-config"  # Optional: custom S3 bucket name for config and cache
  # cache_enabled          = true                   # Optional: enable/disable caching (default: true)
  # config_bucket_kms_key_arn = "arn:aws:kms:..."   # Optional: custom KMS key for encryption (uses AES256 by default)

  s3_bucket_partition_prefix     = "logs/"
  s3_bucket_name_for_audit_entry = "fivexl-sso-elevator"

  s3_mfa_delete  = false
  s3_object_lock = true

  s3_object_lock_configuration = {
    rule = {
      default_retention = {
        mode  = "GOVERNANCE"
        years = 1
      }
    }
  }

  # s3_name_of_the_existing_bucket = "sso_elevator_audit_logs_bucket-<some_sha>"
  # If you want to use your own bucket for storing SSO Elevator audit logs (logs about access requests), use the `s3_name_of_the_existing_bucket` variable.
  # If `s3_name_of_the_existing_bucket` is left empty, the module creates a new bucket name based on `s3_bucket_name_for_audit_entry`.
  # In that case, remember to specify `s3_logging` with at least the `target_bucket` key to enable access logging, otherwise, module deployment will fail.
  s3_logging = {
    target_bucket = "some_access_logging_bucket"
    target_prefix = "some_prefix_for_access_logs"
  }

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
