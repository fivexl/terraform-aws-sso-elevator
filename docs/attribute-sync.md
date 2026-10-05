# Attribute-Based Group Sync

Attribute sync keeps IAM Identity Center group membership in line with user attributes (department, title, cost center and so on). It adds members for good, unlike `group_config`, which grants time-limited membership on request.

## How it works

```mermaid
sequenceDiagram
    EventBridge->>Lambda (attribute-syncer): Triggers on schedule (e.g., hourly)
    Lambda (attribute-syncer)->>Identity Store: List groups, resolve managed group names to IDs
    Lambda (attribute-syncer)->>Identity Store: Read all users with their attributes
    Lambda (attribute-syncer)->>Identity Store: Read managed group memberships
    Lambda (attribute-syncer)->>Identity Store: Add matching users; remove non-matching ones (policy "remove")
    Lambda (attribute-syncer)->>S3 Bucket: Write audit entries
    Lambda (attribute-syncer)->>Slack: Send notifications
```

On each run, for every group in `attribute_sync_managed_groups`:

- A user who matches a rule for the group and is not a member is added.
- A member who matches no rule for the group is a *manual assignment*. With policy `remove` (the default) they are removed; with `warn` they stay and are reported.

Groups not listed in `attribute_sync_managed_groups` are never read or changed.

## Configuration

```hcl
module "aws_sso_elevator" {
  # ... existing configuration ...

  attribute_sync_enabled        = true
  attribute_sync_managed_groups = ["Engineering", "Finance", "DevOps"]

  attribute_sync_rules = [
    {
      group_name = "Engineering"
      attributes = {
        department = "Engineering"
        userType   = "Employee"
      }
    },
    {
      group_name = "Finance"
      attributes = { department = "Finance" }
    },
    {
      group_name = "DevOps"
      attributes = {
        department = "Engineering"
        title      = "DevOps Engineer"
      }
    },
  ]

  attribute_sync_manual_assignment_policy = "warn" # default "remove"
  attribute_sync_schedule                 = "rate(1 hour)"
}
```

The `attribute_sync_*` variables, `attribute_syncer_lambda_name` and `identity_store_id` are listed with their defaults in the [inputs table](https://github.com/fivexl/terraform-aws-sso-elevator#inputs).

When `attribute_sync_enabled = true`, `terraform apply` fails if `attribute_sync_managed_groups` or `attribute_sync_rules` is empty, if a rule names a group missing from `attribute_sync_managed_groups`, or if `sso_instance_arn` is set without `identity_store_id`. The check runs as a `local-exec` during apply, not during plan.

## Mapping rules

Each rule has:

- **group_name**: a group display name, listed in `attribute_sync_managed_groups`.
- **attributes**: conditions that must all match (AND).

Several rules for the same group are alternatives (OR): matching any one of them is enough. Attribute names and values are compared case-insensitively. A rule with empty `attributes` passes `terraform apply`, but the syncer then rejects the whole configuration and every run fails without syncing any group.

Attribute names the syncer reads from each Identity Store user:

- `displayName`, `nickName`, `title`, `userType`, `locale`, `timezone`, `preferredLanguage`, `profileUrl`
- `givenName`, `familyName`, `middleName`, `honorificPrefix`, `honorificSuffix`
- Enterprise attributes: `department`, `costCenter`, `organization`, `division`, `employeeNumber`
- External IDs, as `externalId_<issuer>`

Any other name is never read, so the syncer treats it as unset. For example, use `title`, not `jobTitle`, and `userType`, not `employeeType`.

A missing attribute counts as the empty string, so a condition with value `""` matches every user who lacks that attribute, or names one the syncer does not read. Do not use empty values.

A managed group name that does not exist in the Identity Store is logged as an error and its rules are skipped.

## Rolling it out

Under the default `remove` policy, the first run removes every current member of a managed group who does not match its rules, including people you added by hand. To see what would happen first:

1. Enable with `attribute_sync_manual_assignment_policy = "warn"`.
2. Read the Slack notifications and audit entries for manual assignments; fix the rules or the users' attributes.
3. Switch to `remove`.

Under `warn`, a user who stops matching (for example, changes department) stays in the group, and is reported on every run until removed by hand.

To turn the feature off, set `attribute_sync_enabled = false` and apply. The Lambda and its schedule are deleted; group memberships and audit entries stay as they are.

## Audit entries

Each action is written to the audit bucket with one of these operation types:

- `sync_add`: user added because they match a rule.
- `sync_remove`: member removed because they match no rule (policy `remove`).
- `manual_detected`: member matches no rule and was left in place (policy `warn`), written on every run.

## Slack notifications

The syncer posts to `slack_channel_id`: one message per user added, per manual assignment detected, and per manual assignment removed, plus a summary and any errors at the end of a run that changed something or hit an error. Runs with no changes post nothing.

An error on one user or group does not stop the run; it is counted and reported in the summary. A failed `DescribeUser` call is the exception: it is only logged, and the syncer judges that user from their `ListUsers` record, which lacks the enterprise attributes such as `department`. Under `remove`, a member who matched on those attributes can be removed that run.

## Do not overlap with `group_config`

A managed group must not also appear in `group_config`. The revoker treats every member of a `group_config` group that it has no scheduled revocation for as an inconsistent assignment: it reports it in Slack and removes it on its scheduled revocation run. The syncer then adds the user back on its next run, and the two keep undoing each other. The module does not catch this: its overlap check compares `group_config` resources (group IDs) with managed group names, so it never matches.
