# Architecture

## Components

- **access-requester** Lambda, behind one REST API ([api-gateway.md](api-gateway.md)). It serves Slack and the [CLI](cli.md), decides on requests, grants access and schedules its removal.
- **access-revoker** Lambda. It removes access when a schedule fires, sweeps for assignments nobody scheduled, and handles request expiry and approver reminders.
- **attribute-syncer** Lambda, only with `attribute_sync_enabled = true` ([attribute-sync.md](attribute-sync.md)).
- **EventBridge Scheduler** one-time schedules (group `schedule_group_name`): one per grant to revoke it, plus per pending request a reminder for approvers and an expiry that removes the Approve/Discard buttons after `request_expiration_hours`.
- **EventBridge rules** that start the revoker on `schedule_expression` (revocation sweep, default nightly at 23:00 UTC) and `schedule_expression_for_check_on_inconsistency` (warning only, default every 2 hours).
- **Config bucket** (`config_s3_bucket_name` output): the approval rules (`config/approval-config.json`, written by Terraform) and the requester's cache. Approval rules live here rather than in Lambda environment variables because those are capped at 4 KB in total, which large rule sets exceed.
- **Audit bucket** ([audit.md](audit.md)).
- **SSM parameters** holding the Slack bot token and signing secret ([slack.md](slack.md)).

## Request flow

1. **Intake.** In Slack, the requester opens the `access` or `group-access` shortcut and submits the form; Slack posts it to `POST /access-requester`, and the Lambda checks Slack's signature. With the CLI, `elevator` signs the request with the caller's SSO session and posts it to `POST /access-requester-cli`; the Lambda proves the caller's identity through STS and maps it to an Identity Store user and then a Slack user. The CLI requests account access only, not group membership.
2. **Decision.** The approval rules decide: deny (no matching statement, requester not allowed, or no approvers), grant at once (approval not required, or self-approval), or ask for approval. See [configuration.md](configuration.md) and the [decision diagram](Diagram_of_processing_a_request.png). Every request, from either path, is posted to `slack_channel_id`, so approvers see CLI requests in the same place.
3. **Approval.** An approver clicks Approve or Discard. Until then the revoker reminds approvers with growing intervals (`approver_renotification_*`), and after `request_expiration_hours` it removes the buttons and records the request as `Expired`.
4. **Grant.** The requester creates a user-level account assignment (or adds the user to the group), writes a `grant` audit entry and creates a one-time schedule for the revocation. A new grant for the same assignment replaces the earlier schedule, extending the access.
5. **Revocation.** When the schedule fires, the revoker deletes the assignment (or membership), writes a `revoke` audit entry and updates Slack.
6. **Sweep.** The nightly revoker run deletes every user-level assignment, in the accounts and permission sets the rules name, that has no pending revocation schedule, and removes every member of a configured group (`group_config`) who has no pending revocation schedule. The 2-hourly check only warns in Slack about them.

The sweep is the safety net for everything else: an assignment made by hand, a grant whose revocation could not be scheduled, or a scheduled revocation that failed (the one-time schedule deletes itself after firing, and the revoker's async invoke is not retried). In each case access ends at the next sweep at the latest.

## Outage behaviour

The requester calls Organizations, IAM Identity Center and the Identity Store on every request. To keep working through throttling or a short outage of those APIs, it caches the account list, the permission set list and the Identity Store user list in the config bucket (`cache_enabled`, on by default):

- Each lookup calls the API and reads the cache in parallel. The API answer wins whenever there is one; the cache is rewritten when the answer differs.
- If the API fails, the cached copy is used and a warning is logged. If both fail, the request fails.
- The cache never expires: a stale list is better than none during an outage, and every successful call refreshes it.
- An empty API answer never overwrites a non-empty cache; an empty account or user list is more likely an API fault than the truth.
- A cache read that takes longer than 5 seconds is abandoned, so a slow S3 never delays a request whose API call already returned.
- Cache failures never fail a request; they are logged as warnings (`Failed to get cached ...`, `Failed to cache ...`).

The revoker and attribute-syncer never use the cache: removing access must work from current data, not a snapshot.

Other dependencies, and what an outage of each does:

- **Config bucket, at cold start.** The requester and revoker read the approval rules when they start (or, with SnapStart, after each restore). If the read fails, the Lambda fails to start rather than run with no rules. Warm environments keep working.
- **SSM or Slack, at requester cold start.** The requester reads both Slack secrets and calls Slack's `auth.test` before serving anything, so CLI requests fail too while Slack is unreachable for a cold start. The revoker and attribute-syncer read the bot token per invocation and carry on without Slack if it fails: access is still removed.
- **Audit bucket.** Audit writes never block a grant's scheduling or a revocation; lost entries go to the Lambda's CloudWatch logs. See [audit.md](audit.md) and [accepted risks](accepted-risks.md#an-s3-outage-loses-audit-records-from-the-bucket).
- **EventBridge Scheduler.** If the revocation schedule cannot be created after the grant, the request is reported as failed in Slack, an `incomplete` audit entry names the failed step, and the nightly sweep removes the access.
