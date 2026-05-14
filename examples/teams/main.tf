provider "aws" {
  region = "eu-central-1"

  # Make it faster by skipping something
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_credentials_validation = true
  skip_requesting_account_id  = true
}


data "aws_ssoadmin_instances" "this" {}

# ---------------------------------------------------------------------------
# Microsoft Teams example
#
# Before deploying:
# 1. Follow the "Microsoft Teams app creation" section in the root README.
# 2. Store credentials in SSM Parameter Store (dummy values are fine initially;
#    update after the Teams app is created and you have real values).
# 3. After `terraform apply`, copy `requester_api_endpoint_url` into the bot
#    messaging endpoint in Teams Developer Portal and save.
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "teams_app_id" {
  name = "/sso-elevator/teams-app-id"
}

data "aws_ssm_parameter" "teams_app_password" {
  name            = "/sso-elevator/teams-app-password"
  with_decryption = true
}

module "aws_sso_elevator" {
  source = "../.."

  chat_platform = "teams"

  # Teams bot credentials
  teams_microsoft_app_id         = data.aws_ssm_parameter.teams_app_id.value
  teams_microsoft_app_password   = data.aws_ssm_parameter.teams_app_password.value
  teams_azure_tenant_id          = var.teams_azure_tenant_id
  teams_approval_conversation_id = var.teams_approval_conversation_id

  aws_sns_topic_subscription_email = "email@corp.com"

  sso_instance_arn = one(data.aws_ssoadmin_instances.this.arns)

  schedule_expression                            = "cron(0 23 * * ? *)"
  schedule_expression_for_check_on_inconsistency = "rate(1 hour)"
  revoker_post_update_to_slack                   = true

  approver_renotification_initial_wait_time  = 15
  approver_renotification_backoff_multiplier = 2

  s3_bucket_partition_prefix     = "logs"
  s3_bucket_name_for_audit_entry = "my-sso-elevator-audit"
  s3_mfa_delete                  = false
  s3_object_lock                 = true
  s3_object_lock_configuration = {
    rule = {
      default_retention = {
        mode  = "GOVERNANCE"
        years = 1
      }
    }
  }
  s3_logging = {
    target_bucket = "my-access-logs-bucket"
    target_prefix = "sso-elevator/"
  }

  # Account-level access config.
  # Users request access via /access command in Teams.
  config = [
    # Dev/stage: any of the listed approvers can approve; self-approval allowed
    {
      "ResourceType" : "Account",
      "Resource" : ["dev_account_id", "stage_account_id"],
      "PermissionSet" : "*",
      "Approvers" : ["bob@corp.com", "alice@corp.com"],
      "AllowSelfApproval" : true,
    },
    # Prod read-only: no approval required
    {
      "ResourceType" : "Account",
      "Resource" : ["prod_account_id"],
      "PermissionSet" : "ReadOnly",
      "ApprovalIsNotRequired" : true,
    },
    # Prod admin: two approvers required, no self-approval
    {
      "ResourceType" : "Account",
      "Resource" : ["prod_account_id"],
      "PermissionSet" : "AdministratorAccess",
      "Approvers" : ["manager@corp.com", "ciso@corp.com"],
      "ApprovalIsNotRequired" : false,
      "AllowSelfApproval" : false,
    },
  ]

  # Group-level access config.
  # Users request group membership via /group-access command in Teams.
  # Resource values are IAM Identity Center group IDs (UUIDs).
  group_config = [
    {
      "Resource" : ["99999999-8888-7777-6666-555555555555"], # ManagementAccountAdmins
      "Approvers" : ["admin@corp.com"],
      "ApprovalIsNotRequired" : true,
    },
    {
      "Resource" : ["11111111-2222-3333-4444-555555555555"], # ProdReadOnly
      "Approvers" : ["manager@corp.com"],
      "AllowSelfApproval" : true,
    },
    {
      "Resource" : ["44445555-3333-2222-1111-555557777777"], # ProdAdminAccess
      "Approvers" : ["ciso@corp.com"],
    },
  ]

  # Optional: warn requesters and approvers when a sensitive account is selected.
  # If not set, the module works normally without any warnings.
  # account_warning_messages = {
  #   "123456789012" = "Production — all changes require a change-management ticket."
  #   "210987654321" = "Billing account — read-only access only."
  # }
}


output "requester_api_endpoint_url" {
  description = "Set this URL as the bot messaging endpoint in Teams Developer Portal."
  value       = module.aws_sso_elevator.requester_api_endpoint_url
}
