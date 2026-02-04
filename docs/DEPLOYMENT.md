# Deployment

This document covers deploying SSO Elevator to your AWS environment.

## SSO Delegation

The main reason to delegate SSO to another account is to reduce the need to access the management account, as well as separation of concerns. With a separate SSO management account you can granularly give access to SSO management only without creating an overly complex role in the management account.

Although the module can be deployed in either the management account or the delegated SSO administrator account, we recommend deploying it in the delegated SSO administrator account.

### Setting Up Delegation

To delegate SSO administration, create a new AWS account (if you don't already have one) and delegate SSO administration to it. For more details, refer to the [AWS documentation](https://docs.aws.amazon.com/singlesignon/latest/userguide/delegated-admin-how-to-register.html).

Alternatively, you can use this Terraform snippet in your management account:

```hcl
resource "aws_organizations_delegated_administrator" "sso" {
  account_id        = <<DELEGATED_ACCOUNT_ID>>
  service_principal = "sso.amazonaws.com"
}
```

### Important Limitations

The delegated SSO administrator account **cannot** manage access to the management account. Any permission set created and managed by the management account can't be used by the SSO tooling account.

This means you won't be able to use an `account_level` SSO elevator to manage access to the management account if the elevator is deployed in the delegated SSO administrator account.

### Workaround for Management Account Access

There is still a way to provide **temporary** access to the management account through SSO Elevator:

1. Go to the management account and create a `ManagementAccountAccess` group and permission set (with required permissions).
2. From the management account, assign the `ManagementAccountAccess` group and permission set to the management account.
3. Use SSO Elevator's `/group_access` to request access to this `ManagementAccountAccess` group, which will add you to the group and grant you access to the management account.

---

## Build Process

There are three ways to build SSO Elevator:

### Option 1: Pre-created images from ECR (Default)

Uses pre-built Docker images pulled from ECR. This is the default and recommended approach.

### Option 2: Local Docker build

Build images locally by setting:

```hcl
use_pre_created_image = false
```

### Option 3: Self-hosted ECR

Host ECR yourself by providing:

```hcl
ecr_repo_name = "example_repo_name"
ecr_owner_account_id = "<example_account_id>"
```

### Regional Availability

Images are replicated in every region that AWS SSO supports except:

- ap_east_1
- eu_south_1
- ap_southeast_3
- af_south_1
- me_south_1
- il_central_1
- me_central_1
- eu_south_2
- ap_south_2
- eu_central_2
- ap_southeast_4
- ca_west_1
- us_gov_east_1
- us_gov_west_1

These regions are not enabled by default. If you need support for an unsupported region, please create an issue.

---

## Terraform Deployment Example

```terraform
data "aws_ssoadmin_instances" "this" {}

# Create /sso-elevator/slack-signing-secret AWS SSM Parameter
# and store Slack app signing secret there
data "aws_ssm_parameter" "sso_elevator_slack_signing_secret" {
  name = "/sso-elevator/slack-signing-secret"
}

# Create /sso-elevator/slack-bot-token AWS SSM Parameter
# and store Slack bot token there
data "aws_ssm_parameter" "sso_elevator_slack_bot_token" {
  name = "/sso-elevator/slack-bot-token"
}

module "aws_sso_elevator" {
  source = "github.com/PostHog/terraform-aws-sso-elevator"

  slack_signing_secret = data.aws_ssm_parameter.sso_elevator_slack_signing_secret.value
  slack_bot_token      = data.aws_ssm_parameter.sso_elevator_slack_bot_token.value
  slack_channel_id     = local.slack_channel_id

  # Recommended: Pass identity_store_id to reduce API calls
  identity_store_id = tolist(data.aws_ssoadmin_instances.this.identity_store_ids)[0]

  # S3 logging configuration
  s3_logging = {
    target_bucket = module.naming_conventions.s3_access_logs_bucket_name
    target_prefix = "sso-elevator-logs/"
  }

  s3_bucket_partition_prefix = "sso-elevator-logs"

  # Optional: Object lock for compliance
  s3_object_lock = true
  s3_object_lock_configuration = {
    rule = {
      default_retention = {
        mode  = "GOVERNANCE"
        years = 3
      }
    }
  }

  # Configuration rules
  config = [
    # Dev/Stage self-service
    {
      "ResourceType" : "Account",
      "Resource" : ["dev_account_id", "stage_account_id"],
      "PermissionSet" : "*",
      "Approvers" : ["bob@corp.com", "alice@corp.com"],
      "AllowSelfApproval" : true,
    },
    # Finance billing access
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : "Billing",
      "Approvers" : "finances@corp.com",
      "AllowSelfApproval" : true,
    },
    # CTO full access
    {
      "ResourceType" : "Account",
      "Resource" : "*",
      "PermissionSet" : "*",
      "Approvers" : "cto@corp.com",
      "AllowSelfApproval" : true,
    },
    # Prod read-only
    {
      "ResourceType" : "Account",
      "Resource" : ["prod_account_id", "prod_account_id2"],
      "PermissionSet" : "ReadOnly",
      "AllowSelfApproval" : true,
    },
    # Prod admin (strict approval)
    {
      "ResourceType" : "Account",
      "Resource" : ["prod_account_id", "prod_account_id2"],
      "PermissionSet" : "AdministratorAccess",
      "Approvers" : ["manager@corp.com", "ciso@corp.com"],
      "ApprovalIsNotRequired" : false,
      "AllowSelfApproval" : false,
    },
  ]

  # Optional: Group configuration
  group_config = [
    {
      "Resource" : ["99999999-8888-7777-6666-555555555555"],
      "Approvers" : ["email@gmail.com"]
      "ApprovalIsNotRequired": true
    },
  ]
}

output "aws_sso_elevator_api_endpoint_url" {
  value = module.aws_sso_elevator.requester_api_endpoint_url
}
```

---

## Slack App Creation

### Step 1: Create the App

1. Go to https://api.slack.com/
2. Click `Create an app`
3. Click `From an app manifest`
4. Select your workspace, click `Next`
5. Choose `yaml` for app manifest format

### Step 2: Configure the Manifest

Update the `request_url` with the value from `requester_api_endpoint_url` Terraform output:

```yaml
display_information:
  name: AWS SSO Access Elevator
  description: Slack bot to temporary assign AWS SSO Permission set to a user
features:
  bot_user:
    display_name: AWS SSO Access Elevator
    always_online: false
  shortcuts:
    - name: access
      type: global
      callback_id: request_for_access
      description: Request access to Permission Set in AWS Account
    - name: group-access
      type: global
      callback_id: request_for_group_membership
      description: Request access to SSO Group
oauth_config:
  scopes:
    bot:
      - commands
      - chat:write
      - users:read.email
      - users:read
      - channels:history
      - channels:read
      - groups:read
      - im:write
settings:
  interactivity:
    is_enabled: true
    request_url: <API GATEWAY URL FROM requester_api_endpoint_url OUTPUT>
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

> **Note**: Remove the `group-access` shortcut if you want to disable Group Assignments Mode.

### Step 3: Install and Configure

1. Click `Create`
2. Click `Install to Workspace`
3. Copy the `Signing Secret` - use for `slack_signing_secret` module input
4. Copy the `Bot User OAuth Token` - use for `slack_bot_token` module input

### Slack Permissions Reference

| Permission | Purpose |
|------------|---------|
| `commands` | Add shortcuts and slash commands |
| `chat:write` | Post messages to Slack |
| `users:read.email` | Find user's email address for AWS account assignments |
| `users:read` | Read user info for mentions in requests |
| `channels:history` | Find old messages for request expiration events |
| `channels:read` | Determine if requester is in the channel |
| `groups:read` | Same as above but for private channels |
| `im:write` | Send direct messages to users not in the channel |
