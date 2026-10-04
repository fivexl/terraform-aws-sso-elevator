# Deployment and Usage

The deployment process is divided into two main parts: deploying the Terraform module, which sets up the necessary infrastructure and resources for the Lambdas to function, and creating a Slack App, which will be the interface through which users can interact with the Lambdas. Detailed instructions on how to perform both of these steps, along with the Slack App manifest, can be found below.

## SSO Delegation
AWS recommends delegating SSO administration to a separate “delegated SSO administrator account.” We also recommend creating a dedicated “sso-tooling” account to manage access across your entire organization. You can learn more about how to use SSO Elevator in the delegated SSO administrator account here: [SSO delegation](/docs/docs.md#sso-delegation)

## Build Process
There are three ways to build an SSO elevator:

Using pre-created images pulled from ECR (Default)
Using Docker build to build images locally (provide the variable use_pre_created_image = false)
There is also an option to host ECR yourself by providing the following variables:
```hcl
ecr_repo_name = "example_repo_name"
ecr_owner_account_id = "<example_account_id>"
```

GitHub CI of this repository pre-builds the requester and revoker lambda Docker images on every release and push them to FivexL's private ECR. Users can use these pre-built Docker images to build lambdas.

## Terraform deployment example

```terraform

data "aws_ssoadmin_instances" "this" {}

module "aws_sso_elevator" {
  source  = "fivexl/sso-elevator/aws"
  version = "5.0.0"
  # The Slack bot token and signing secret live in SSM; see "Slack secrets in SSM Parameter Store".
  slack_channel_id = local.slack_channel_id

  s3_logging = {
    target_bucket = module.naming_conventions.s3_access_logs_bucket_name
    target_prefix = "sso-elevator-logs/"
  }

  s3_bucket_partition_prefix = "sso-elevator-logs"

  s3_object_lock = true
  s3_object_lock_configuration = {
    rule = {
      default_retention = {
        mode  = "GOVERNANCE"
        years = 3
      }
    }
  }
  # The default object lock configuration is as follows:
  # {
  #  rule = {
  #   default_retention = {
  #      mode  = "GOVERNANCE"
  #      years = 2
  #    }
  #  }
  #}
  # You can specify a different configuration here:
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

  config = [
    # This could be a config for dev/stage account where developers can self-serve
    # permissions
    # Allows Bob and Alice to approve requests for all
    # PermissionSets in accounts dev_account_id and stage_account_id as
    # well as approve its own requests
    # You have to specify at AllowSelfApproval: true or specify two approvers
    # so you do not lock out approver
    {
      "ResourceType" : "Account",
      "Resource" : ["dev_account_id", "stage_account_id"],
      "PermissionSet" : "*",
      "Approvers" : ["bob@corp.com", "alice@corp.com"],
      "AllowSelfApproval" : true,
    },
    # This could be an option for a financial person
    # allows self approval for Billing PermissionSet
    # for account_id for user finances@corp.com
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : "Billing",
      "Approvers" : "finances@corp.com",
      "AllowSelfApproval" : true,
    },
    # Your typical CTO - can approve all accounts and all permissions
    # as well as his/hers own requests to avoid lock out
    # Careful withi Resource * since it will cause revocation of all
    # non-module-created user-level permission set assignments in all
    # accounts, add this one later when you are done with single account
    # testing
    {
      "ResourceType" : "Account",
      "Resource" : "*",
      "PermissionSet" : "*",
      "Approvers" : "cto@corp.com",
      "AllowSelfApproval" : true,
    },
    # Read only config for production accounts so developers
    # can check prod when needed
    {
      "ResourceType" : "Account",
      "Resource" : ["prod_account_id", "prod_account_id2"],
      "PermissionSet" : "ReadOnly",
      "AllowSelfApproval" : true,
    },
    # Prod access
    {
      "ResourceType" : "Account",
      "Resource" : ["prod_account_id", "prod_account_id2"],
      "PermissionSet" : "AdministratorAccess",
      "Approvers" : ["manager@corp.com", "ciso@corp.com"],
      "ApprovalIsNotRequired" : false,
      "AllowSelfApproval" : false,
    },
    # example of list being used for permissions sets
    {
      "ResourceType" : "Account",
      "Resource" : "account_id",
      "PermissionSet" : ["ReadOnlyPlus", "AdministratorAccess"],
      "Approvers" : ["ciso@corp.com"], 
      "AllowSelfApproval" : true,
    },

  ]
group_config = [
    {              
      "Resource" : ["99999999-8888-7777-6666-555555555555"], #ManagementAccountAdmins
      "Approvers" : [
        "email@gmail.com"
      ]
      "ApprovalIsNotRequired": true
    },
    {              
      "Resource" : ["11111111-2222-3333-4444-555555555555"], #prod read only
      "Approvers" : [
        "email@gmail.com"
      ]
      "AllowSelfApproval" : true,
    },
    {
      "Resource" : ["44445555-3333-2222-1111-555557777777"], #ProdAdminAccess
      "Approvers" : [
        "email@gmail.com"
      ]
    },
]
}

output "requester_api_endpoint_url" {
  value = module.aws_sso_elevator.requester_api_endpoint_url
}
```

## SSO Delegation

The main reason to delegate SSO to another account, is to reduce a need to access management account to the minimum as well as separation of concerns. With a separate SSO management account you can granualary give access to sso management only without creating overcomplex role in the management account that would limit access in the management account to SSO only.

Although the module can be deployed in either the management account or the delegated SSO administrator account, we recommend deploying it in the delegated SSO administrator account.

To do this, create a new AWS account (if you don’t already have one) and and delegate SSO administration to it. For more details on this process, refer to the [AWS documentation](https://docs.aws.amazon.com/singlesignon/latest/userguide/delegated-admin-how-to-register.html).

Alternatively, you can use this Terraform snippet in your management account to delegate SSO permissions to the new account:

```hcl
resource "aws_organizations_delegated_administrator" "sso" {
  account_id        = <<DELEGATED_ACCOUNT_ID>>
  service_principal = "sso.amazonaws.com"
}
```
This is only pre-requisite for the module to work in the delegated SSO administrator account. After this step, you can proceed with the module deployment.

**Important Note:**

The delegated SSO administrator account **cannot** be used to manage access to the management account. Specifically, any permission set created and managed by the management account can’t be used by the SSO tooling account. (If you create a permission set in the Management account and try to use it in the SSO account, you’ll get an “Access Denied” error.)

This limitation ensures that the management account always manages access to itself, while the delegated SSO administrator account manages access to every other account in the organization. As a result, you won’t be able to use an `account_level` SSO elevator to manage access to the management account if the elevator is deployed in the delegated SSO administrator account.

However, there is still a way to provide **temporary** access to the management account through SSO Elevator:

1. Go to the management account and create a `ManagementAccountAccess` group and permission set (with required permissions).
2. From the management account, assign the `ManagementAccountAccess` group and permission set to the management account.
3. Use SSO Elevator to `/group_access` request access to this `ManagementAccountAccess` group, which will add you to the group and grant you access to the management account. (this way you don't directly use the permission set, so you don't hit the limitation and get access to the management account)

With this approach, you can reduce how often you use the management account and how many resources you deploy there, while still being able to manage the entire organization and temporarily access the management account.
