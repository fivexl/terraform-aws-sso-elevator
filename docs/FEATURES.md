# Features

This document covers additional features available in SSO Elevator.

## Secondary Subdomain Fallback

> **WARNING**: This feature is STRONGLY DISCOURAGED because it can introduce security risks.

SSO Elevator uses Slack email addresses to find users in AWS SSO. In some cases, the domain of a Slack user's email (e.g., "john.doe@old.domain") differs from the domain defined in AWS SSO (e.g., "john.doe@new.domain"). By setting fallback domains, SSO Elevator will attempt to replace the original domain from Slack with each secondary domain to locate a matching AWS SSO user.

### Use Case

- Slack email: john.doe@old.domain
- AWS SSO email: john.doe@new.domain

Without fallback domains, SSO Elevator cannot find the SSO user. By setting `secondary_fallback_email_domains = ["@new.domain"]`, SSO Elevator will swap "@old.domain" for "@new.domain" and attempt to locate "john.doe@new.domain" in AWS SSO.

### Configuration

```hcl
secondary_fallback_email_domains = ["@new.domain", "@second.domain"]
```

### Security Risks

- If multiple SSO users share the same local-part (before the "@") across different domains, SSO Elevator may grant permissions to the wrong user.
- Disable or remove entries as soon as you no longer need domain fallback functionality.

### Important Notes

- SSO Elevator always prioritizes the primary domain from Slack when searching for a user in AWS SSO.
- A large warning message is added in Slack if a secondary fallback domain is used.
- The secondary domain feature works **ONLY** for the requester; approvers must have the same email domain in Slack as in AWS SSO.

---

## Direct Messages Feature

SSO Elevator uses Slack channels to communicate with users. In some setups, only approvers are members of the channel, so requesters don't receive feedback about their requests.

To solve this, SSO Elevator can send direct messages to users if they are not in the channel.

### Configuration

```hcl
send_dm_if_user_not_in_channel = true
```

### Required Slack Permissions

Your Slack app must have these permissions:

- `channels:read` - View basic information about public channels
- `groups:read` - View basic information about private channels the app has been added to
- `usergroups:read` - Resolve Slack user group members for approver groups
- `im:write` - Send direct messages to workspace members

---

## API Gateway

The module uses API Gateway to expose the Lambda function to Slack. This avoids the [lambda-1](https://docs.aws.amazon.com/securityhub/latest/userguide/lambda-controls.html#lambda-1) SecurityHub control alert that would be triggered by using Lambda function URLs with a FunctionURLAllowPublicAccess resource-based policy.

### Configuration

API Gateway is enabled by default. The endpoint URL is available in the `requester_api_endpoint_url` output. Use this URL as the Request URL in your Slack App manifest.

```hcl
# Optional: Customize throttling
api_gateway_throttling_burst_limit = 5
api_gateway_throttling_rate_limit = 1
```

---

## Request Expiration

Requests can be configured to automatically expire after a set number of hours if not approved.

### Configuration

```hcl
request_expiration_hours = 8  # Requests expire after 8 hours
```

Set to `0` to disable expiration (requests never expire).

---

## Early Session Revocation

Users can end their access session early before the scheduled expiration using the "End session early" button.

### Configuration

```hcl
# By default, only the requester and approvers can end sessions early
allow_anyone_to_end_session_early = false

# Set to true to allow anyone in the channel to end any session
allow_anyone_to_end_session_early = true
```

---

## Approver Re-notification

SSO Elevator can re-notify approvers if a request hasn't been acted upon.

### Configuration

```hcl
# Initial wait time before first re-notification (in minutes)
# Set to 0 to disable re-notifications
approver_renotification_initial_wait_time = 15

# Multiplier for subsequent notifications (doubles wait time by default)
approver_renotification_backoff_multiplier = 2
```

---

## Provisioned Concurrency

To reduce Lambda cold starts for the Slack handler, you can configure provisioned concurrency.

### Configuration

```hcl
# Set to a positive number to enable
slack_handler_provisioned_concurrent_executions = 1
```

---

## Caching

SSO Elevator caches AWS accounts and permission sets in S3 to reduce API calls.

### Configuration

```hcl
# Enabled by default
cache_enabled = true

# Custom bucket name
config_bucket_name = "sso-elevator-config"
```

---

## Audit Logging

All access grants and revocations are logged to S3 for auditing purposes.

### Configuration

```hcl
# Use an existing bucket
s3_name_of_the_existing_bucket = "my-audit-bucket"

# Or let the module create one
s3_bucket_name_for_audit_entry = "sso-elevator-audit-entry"

# Enable object lock for compliance
s3_object_lock = true
s3_object_lock_configuration = {
  rule = {
    default_retention = {
      mode  = "GOVERNANCE"
      years = 2
    }
  }
}
```

See [Athena Query documentation](../athena_query/) for information on querying audit logs.

---

## Inconsistency Detection

The Access-Revoker continuously reconciles the revocation schedule with all user-level permission set assignments and issues warnings if it detects assignments without a revocation schedule (presumably created manually).

By default, the Access-Revoker will automatically revoke all unknown user-level permission set assignments daily.

### Configuration

```hcl
# How often to revoke unknown assignments
schedule_expression = "cron(0 23 * * ? *)"

# How often to check and warn about inconsistencies
schedule_expression_for_check_on_inconsistency = "rate(2 hours)"
```

---

## Analytics

SSO Elevator can send usage analytics to PostHog. This is disabled by default.

### Configuration

```hcl
posthog_api_key = "phc_..."  # Leave empty to disable
posthog_host    = "https://us.i.posthog.com"  # Optional, this is the default
```

### Events

All events include `application: "aws-sso-elevator"` as a property.

| Event | Trigger | Properties |
|-------|---------|------------|
| `aws_access_requested` | User submits access request | account_id, permission_set, requester_email, granted, duration_hours |
| `aws_access_approved` | Access approved | account_id, permission_set, approver_email, duration_hours, self_approved |
| `aws_access_denied` | Request denied | account_id, permission_set, approver_email, requester_email |
| `aws_access_revoked_early` | Early revocation | account_id, permission_set, revoker_email, reason |
| `aws_group_access_requested` | Group access request | group_id, group_name, requester_email |
| `aws_group_access_approved` | Group access approved | group_id, group_name, duration_hours, self_approved |
| `aws_sso_elevator_error` | Error occurs | error_type, error_message |
