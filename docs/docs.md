## SSO Delegation

The main reason to delegate SSO to another account, is to reduce a need to access management account to the minimum as well as separation of concerns. With a separate SSO management account you can granualary give access to sso management only wihtout creating overcomplex role in the management account that would limit access in the management account to SSO only.

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
