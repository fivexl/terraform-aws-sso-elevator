# Configuration

## Group Assignments Mode
Starting from version 2.0, Terraform AWS SSO Elevator introduces support for group access. SSO elevator now can add users to a groups, to do so, you will need to use /group-access command, which, instead of showing the form for account assignments, will present a Slack form where the user can select a group they want access to, specify a reason, and define the duration for which access is required.

The basic logic for access, configuration, and Slack integration remains the same as before. To enable the new Group Assignments Mode, you need to provide the module with a new group_config Terraform variable:
```hcl
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
```
There are two key differences compared to the standard Elevator configuration:
- ResourceType is not required for group access configurations.
- In the Resource field, you must provide group IDs instead of account IDs.

Group statements also support the optional `AllowedGroups` and `AllowedUsers` requester restrictions — see [Restricting who can request access](#restricting-who-can-request-access).

The Elevator will only work with groups specified in the configuration.

If you were using Terraform AWS SSO Elevator before version 2.0.0, you need to update your Slack app manifest by adding a new shortcut to enable this functionality:
{
    "name": "group-access",
    "type": "global",
    "callback_id": "request_for_group_membership",
    "description": "Request access to SSO Group"
}
To disable this functionality, simply remove the shortcut from the manifest.

## Module configuration, and features

### Configuration structure

The configuration is a list of dictionaries, where each dictionary represents a single configuration rule.

Each configuration rule specifies which resource(s) the rule applies to, which permission set(s) are being requested, who the approvers are, and any additional options for approving the request.

The fields in the configuration dictionary are:

- **ResourceType**: This field specifies the type of resource being requested, such as "Account." As of now, the only supported value is "Account."
- **Resource**: This field defines the specific resource(s) being requested. It accepts either a single string or a list of strings. Setting this field to "*" allows the rule to match all resources associated with the specified `ResourceType`.
- **PermissionSet**: Here, you indicate the permission set(s) being requested. This can be either a single string or a list of strings. If set to "*", the rule matches all permission sets available for the defined `Resource` and `ResourceType`.
- **Approvers**: This field lists the potential approvers for the request. It accepts either a single string or a list of strings representing different approvers.
- **AllowSelfApproval**: This field can be a boolean, indicating whether the requester, if present in the `Approvers` list, is permitted to approve their own request. It defaults to `None`.
- **ApprovalIsNotRequired**: This field can also be a boolean, signifying whether the approval can be granted automatically, bypassing the approvers entirely. The default value is `None`.
- **AllowedGroups**: Optional requester restriction. A single SSO group ID or a list of SSO group IDs. If set, only members of at least one of the listed groups may request access using this statement. See [Restricting who can request access](#restricting-who-can-request-access).
- **AllowedUsers**: Optional requester restriction. A single email or a list of emails. If set, only the listed users may request access using this statement. See [Restricting who can request access](#restricting-who-can-request-access).

#### Restricting who can request access

By default, a statement says what can be requested and who approves it, but not who is allowed to request it — any user the Elevator can resolve in SSO can request any account/permission set (or group) that has a matching statement. The optional `AllowedGroups` and `AllowedUsers` fields restrict the requester side. They work the same way in both `config` (account statements) and `group_config` (group statements):

- If both fields are omitted or empty, the statement is unrestricted — same behavior as before.
- If either field is set, the requester must be a member of at least one group listed in `AllowedGroups` **or** be listed by email in `AllowedUsers`. Matching either one is sufficient.
- `AllowedGroups` entries are SSO group IDs (the same format as `Resource` in `group_config`). Group membership is resolved via the IAM Identity Center `ListGroupMembershipsForMember` API using the requester's SSO user.
- `AllowedUsers` entries are matched against the requester's email case-insensitively, including the secondary fallback domain variants if `secondary_fallback_email_domains` is configured.
- The restriction applies to the whole statement, including `ApprovalIsNotRequired` and `AllowSelfApproval` — an ineligible requester cannot use an auto-approved statement.
- It is enforced at both request time and approval time, so an ineligible request can't be approved either.
- If the requester cannot be resolved to an SSO user or group memberships can't be fetched, processing stops with an error and no access is granted.
- If statements match the request but the requester is not eligible for any of them, the request is denied with a "not allowed to request" message.
- The Slack request dialogs only show what the requester is eligible for: groups, accounts, and permission sets covered solely by statements the requester can't use are hidden from the select lists. If nothing is available, the dialog shows a "not allowed to request access" message instead of the form. (Note: account and permission-set lists are filtered independently, so a specific ineligible account/permission-set *combination* may still be selectable — it is denied on submission.)

Example — developers can request `ReadOnlyAccess` themselves, but only members of the infra group (or a specific user) can request `AdministratorAccess`:

```hcl
config = [
  {
    "ResourceType" : "Account",
    "Resource" : "*",
    "PermissionSet" : "ReadOnlyAccess",
    "Approvers" : ["lead@example.com"],
  },
  {
    "ResourceType" : "Account",
    "Resource" : "*",
    "PermissionSet" : "AdministratorAccess",
    "Approvers" : ["cto@example.com"],
    "AllowedGroups" : ["99999999-8888-7777-6666-555555555555"], # infra team SSO group
    "AllowedUsers" : ["oncall@example.com"],
  },
]

group_config = [
  {
    "Resource" : ["11111111-2222-3333-4444-555555555555"], # ProdAdmins
    "Approvers" : ["cto@example.com"],
    "AllowedGroups" : ["99999999-8888-7777-6666-555555555555"], # infra team SSO group
    "AllowedUsers" : ["oncall@example.com"],
  },
]
```

#### Explicit Deny
In the system, an explicit denial in any statement overrides any approvals. For instance, if one statement designates an individual as an approver for all accounts, but another statement specifies that the same individual is not allowed to self-approve or to bypass the approval process for a particular account and permission set (by setting "allow_self_approval" and "approval_is_not_required" to `False`), then that individual will not be able to approve requests for that specific account, thereby enforcing a stricter control.

#### Automatic Approval
Requests will be approved automatically if either of the following conditions are met:

- AllowSelfApproval is set to true and the requester is in the Approvers list.
- ApprovalIsNotRequired is set to true.

#### Aggregation of Rules
The approval decision and final list of reviewers will be calculated dynamically based on the aggregate of all rules. If you have a rule that specifies that someone is an approver for all accounts, then that person will be automatically added to all requests, even if there are more detailed rules for specific accounts or permission sets.

#### Single Approver
If there is only one approver and AllowSelfApproval is not set to true, nobody will be able to approve the request.

#### Diagram of processing a request:
![Diagram of processing a request](docs/Diagram_of_processing_a_request.png)

### Secondary Subdomain Fallback Feature:
WARNING: 
This feature is STRONGLY DISCOURAGED because it can introduce security risks.

SSO Elevator uses Slack email addresses to find users in AWS SSO. In some cases, the domain of a Slack user's email 
(e.g., "john.doe@old.domain") differs from the domain defined in AWS SSO (e.g., "john.doe@new.domain"). By setting 
these fallback domains, SSO Elevator will attempt to replace the original domain from Slack with each secondary domain 
in order to locate a matching AWS SSO user. 
 
- This mechanism should only be used in rare or critical situations where you cannot align Slack and AWS SSO domains.

Example:
- Slack email: john.doe@old.domain
- AWS SSO email: john.doe@new.domain

Without fallback domains, SSO Elevator cannot find the SSO user due to the domain mismatch. By setting 
secondary_fallback_email_domains = ["@new.domain"], SSO Elevator will try to swap out "@old.domain" for "@new.domain"
(and any other domain in the list) and attempt to locate "john.doe@new.domain" in AWS SSO.

Security Risks & Recommendations:
- If multiple SSO users share the same local-part (before the "@") across different domains, SSO Elevator may 
  grant permissions to the wrong user.
- Disable or remove entries in this variable as soon as you no longer need domain fallback functionality 
  to restore a more secure configuration.

IN SUMMARY:
Use "secondary_fallback_email_domains" ONLY if absolutely necessary. It is best practice to maintain 
consistent, verified email domains in Slack and AWS SSO. Remove these fallback entries as soon as you 
resolve the underlying domain mismatch to minimize security exposure.

SSO Elevator will update request message in channel with Warning, if fallback domains are in use.

**Upgrade note (4.4.0+):** if two or more Identity Store users share the same email case-insensitively, every request from any of them now fails outright with a clear "email collision" error, instead of silently resolving to whichever of them happened to come first in Identity Store's own listing order. If your directory has such a collision, requests from the affected users will start failing on upgrade until it's resolved on the Identity Store side.

Notes:
- SSO Elevator always prioritizes the primary domain from Slack (the Slack user's email) when searching for a user in AWS SSO.
- SSO Elevator adds a one-line :warning: to the request message in Slack if it uses a secondary fallback domain to find a user in AWS SSO.
- The secondary domain feature works **ONLY** for the requester, approvers in the configuration must have the same email domain as in Slack.

### Sending direct messages to users feature
SSO Elevator uses slack channels to communicate with users. But there is a use case of SSO Elevator where only approvers are members of a channel, so no one except them can see who has access where. And when this is the case, requesters don't get any feedback about their requests. To solve this problem, SSO Elevator can send direct messages to users if they are not in the channel. To enable this feature, your SSO Elevator slack app should have the following permissions: ("channels:read", "groups:read", "im:write"). And `send_dm_if_user_not_in_channel` variable should be set to true. If you are updating from the previous version but for a time being you can't update slack app permissions, you can use `send_dm_if_user_not_in_channel` variable to disable this feature so it won't break your current setup.
