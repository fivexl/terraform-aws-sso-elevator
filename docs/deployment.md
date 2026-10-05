# Deployment

Deploying SSO Elevator has two parts. The Terraform module creates the Lambdas, the API and the audit bucket. A Slack app is the interface through which users request and approve access. [Slack](slack.md) covers the Slack app, its manifest and the Slack secrets; [Configuration](configuration.md) covers approval rules.

## SSO Delegation

Deploy the module in a delegated SSO administrator account rather than the management account. AWS recommends delegating IAM Identity Center administration, and it keeps day-to-day access out of the management account. With a separate account you can grant SSO administration without building a role in the management account that limits itself to SSO. The module works in either account.

To delegate, create an account (we recommend a dedicated "sso-tooling" account). Register it as the IAM Identity Center delegated administrator, as the [AWS documentation](https://docs.aws.amazon.com/singlesignon/latest/userguide/delegated-admin-how-to-register.html) describes, or with this Terraform in the management account:

```hcl
resource "aws_organizations_delegated_administrator" "sso" {
  account_id        = "<DELEGATED_ACCOUNT_ID>"
  service_principal = "sso.amazonaws.com"
}
```

That is the only prerequisite for running the module in the delegated administrator account.

**The delegated administrator cannot manage access to the management account.** Permission sets provisioned to the management account can only be managed from the management account; using one from the delegated account fails with "Access Denied". So an elevator deployed in the delegated account cannot grant account-level access to the management account. To grant temporary access to it anyway:

1. In the management account, create a `ManagementAccountAccess` group and permission set with the permissions you need.
2. From the management account, assign that group and permission set to the management account.
3. Request membership of `ManagementAccountAccess` through SSO Elevator's `group-access` Slack shortcut. The elevator only changes group membership, so it never touches the management account's permission set.

## Lambda Images

The module can get its Lambda code three ways:

- **Pre-built images (default).** Pulled from FivexL's ECR in the deployment region: `<ecr_owner_account_id>.dkr.ecr.<region>.amazonaws.com/<ecr_repo_name>:requester-<ecr_repo_tag>`, plus `revoker-` and (with attribute sync on) `attribute-syncer-` images.
- **Your own ECR.** Set `ecr_owner_account_id` and `ecr_repo_name` to a repository you host, holding images with the same tag names. It must be in the region you deploy to; in another account, its repository policy must let Lambda in the deployment account pull.
- **Built locally.** `use_pre_created_image = false` packages the source as zip files, building them in Docker on the machine running Terraform, so Docker must be available there.

Lambda only runs container images from private ECR in the same region: not from public ECR, other registries or a pull-through cache. That is why the pre-built images are replicated per region.

Pre-built images exist in these 17 regions:

- `eu-central-1` (source)
- `ap-northeast-1`, `ap-northeast-2`, `ap-northeast-3`, `ap-south-1`, `ap-southeast-1`, `ap-southeast-2`
- `ca-central-1`
- `eu-north-1`, `eu-west-1`, `eu-west-2`, `eu-west-3`
- `sa-east-1`
- `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`

In any other region, set `use_pre_created_image = false`, host the images yourself, or [open an issue](https://github.com/fivexl/terraform-aws-sso-elevator/issues) asking for the region.

The pre-built images are built for `x86_64` only. With `lambda_architecture = "arm64"`, build locally or host your own `arm64` images.

Image tags:

- `X.Y.Z`, one per release. The module's `ecr_repo_tag` default is the matching release.
- `main`, the latest merge to `main`. It moves with every merge.
- `pr-<N>-<sha>`, one per push to a pull request from this repository.

`main` and `pr-<N>-<sha>` images are for testing and expire once superseded. Set `ecr_repo_tag` to one of them to try an unreleased change.

## Terraform Example

```hcl
data "aws_ssoadmin_instances" "this" {}

module "aws_sso_elevator" {
  source  = "fivexl/sso-elevator/aws"
  version = "5.0.0"

  slack_channel_id = "C0123456789"
  sso_instance_arn = one(data.aws_ssoadmin_instances.this.arns)

  # Always required: access logging for the config bucket, and for the audit
  # bucket unless you set s3_name_of_the_existing_bucket to use your own.
  s3_logging = {
    target_bucket = "my-s3-access-logs-bucket"
    target_prefix = "sso-elevator/"
  }
  # Object lock defaults to GOVERNANCE mode, 2 years (s3_object_lock_configuration).
  s3_object_lock = true

  config = [
    # Dev and stage: developers self-serve any permission set. Self-approval or
    # two approvers, so an approver cannot lock themselves out.
    {
      "ResourceType" : "Account",
      "Resource" : ["111111111111", "222222222222"],
      "PermissionSet" : "*",
      "Approvers" : ["bob@corp.com", "alice@corp.com"],
      "AllowSelfApproval" : true,
    },
    # Production: read-only for anyone, approved automatically.
    {
      "ResourceType" : "Account",
      "Resource" : ["333333333333"],
      "PermissionSet" : "ReadOnly",
      "ApprovalIsNotRequired" : true,
    },
    # Production admin needs someone else's approval.
    {
      "ResourceType" : "Account",
      "Resource" : ["333333333333"],
      "PermissionSet" : "AdministratorAccess",
      "Approvers" : ["manager@corp.com", "ciso@corp.com"],
    },
    # "Resource": "*" makes the revoker remove every user-level permission set
    # assignment the module did not create, in every account. Add it after
    # testing with a single account. No AllowSelfApproval here: it would let
    # cto@corp.com grant themselves production admin without approval.
    {
      "ResourceType" : "Account",
      "Resource" : "*",
      "PermissionSet" : "*",
      "Approvers" : "cto@corp.com",
    },
  ]

  group_config = [
    {
      "Resource" : ["11111111-2222-3333-4444-555555555555"], # Identity Store group id
      "Approvers" : ["cto@corp.com"],
      "AllowSelfApproval" : true,
    },
  ]
}

output "requester_api_endpoint_url" {
  value = module.aws_sso_elevator.requester_api_endpoint_url
}

output "requester_api_endpoint_url_cli" {
  value = module.aws_sso_elevator.requester_api_endpoint_url_cli
}
```

After the first apply, finish the [Slack fresh install](slack.md#fresh-install).
