# Changelog

Notable changes to the Terraform module, the `elevator` CLI and the Lambda images, which share one version. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [5.0.0] - Unreleased

Breaking release with downtime. Follow [UPGRADE-5.0.md](UPGRADE-5.0.md).

### Added

- One REST API serves both Slack (`POST /access-requester`) and the CLI (`POST /access-requester-cli`), invoking the requester Lambda's `live` alias. A request validator answers `400` to a Slack-route POST without Slack's signature headers, without invoking the Lambda.
- Optional AWS WAF: `waf_enabled` creates a web ACL with a per-IP rate limit (`waf_rate_limit`) and AWS managed rule sets; `waf_web_acl_arn` attaches one you manage.
- Lambda SnapStart on the requester (`snap_start`, on by default). It cuts cold-start latency to help the first Slack click after idle meet Slack's 3-second deadline. The revoker's nightly run deletes old requester versions, keeping `live` and one rollback version.
- Opt-in REST API stage access logs (`api_gateway_access_logs_enabled`).
- The CLI proves its caller's identity with a presigned `sts:GetCallerIdentity` request bound to the request body and API id. The Lambda no longer trusts the identity in the event, so a direct Lambda invoke cannot forge a CLI caller.
- CLI callers from any account in the organization, admitted by the API's resource policy.
- `elevator configure --api-id` and `ELEVATOR_API_ID`, for a CLI that reaches the API through a custom domain.
- Audit entries for requests that end without access (`declined`: auto-denied, discarded, expired) and for approved requests that fail (`incomplete`), with new fields `decision_reason` and `error_message`. Audit entries carry `version` `2`.
- Inputs: `slack_bot_token_ssm_parameter_name`, `slack_signing_secret_ssm_parameter_name`, `snap_start`, `waf_enabled`, `waf_web_acl_arn`, `waf_rate_limit`, `api_gateway_access_logs_enabled`.
- Outputs: `requester_api_id`, `waf_web_acl_arn`.
- Lambda images for pull requests (`pr-<N>-<sha>`) and a moving `main` tag, usable as `ecr_repo_tag` for testing.

### Changed

- Slack secrets live in two SSM SecureString parameters that the module creates with a placeholder through a write-only argument. You set the values with the AWS CLI, so they never reach Terraform state, plan output or Lambda environment variables.
- The CLI route is on by default (`enable_access_requester_cli` default `false` → `true`).
- The CLI requires elevator 5.0.0 or newer; older CLIs get `400` asking to upgrade.
- Redesigned Slack messages: one card per request, the same for Slack and CLI requests, updated in place through every state, including the revocation. The request state lives in the buttons. Reasons are capped at 1,000 characters.
- The `event_bridge_*_rule_name` inputs default to the names the removed `event_brige_*` inputs used.
- IAM tightened: Scheduler actions scoped to the schedule group, `iam:PassRole` only for the scheduler role and only to Scheduler, SAML provider actions scoped to Identity Center's provider. The unused EventBridge Lambda permission and `events:PutRule`/`PutTargets` are gone.
- Lambda async retries after a function error are off for the requester's `live` alias.
- Audit writes never block access expiry: when the grant's audit write fails, revocation is still scheduled, and the entry is logged in full to CloudWatch (#245).
- The Lambdas run on Python 3.14.
- Requires Terraform `~> 1.11` (was `~> 1.0`) and hashicorp/aws `>= 6.28` (was `>= 4.64`).
- Dependencies updated to versions that have soaked for 14 days.

### Removed

- The HTTP API, the Lambda Function URL and the `lambda_function_url` output.
- Inputs `slack_bot_token`, `slack_signing_secret`, `create_api_gateway`, `create_lambda_url`, `cli_sso_role_name_prefix`, `event_brige_check_on_inconsistency_rule_name` and `event_brige_scheduled_revocation_rule_name`.

### Fixed

- Two racing Approve clicks no longer mark a granted request as failed. Revoke schedules created in the same second no longer collide, which left the second grant without automatic revocation.
- The CLI path writes the Identity Store users cache; it failed on every request before.
- The reconciliation sweep skips a group membership that is already gone instead of failing the whole run (#246).
- The revoker's group sweep and inconsistency check use the Identity Store client they are given (#245).
- A Slack failure in the revoker's batch revocation no longer stops the remaining revocations.
- The attribute syncer skips a user whose attributes it cannot read, and reports the failure in its run summary. Before, under the `remove` policy, it could remove a member who matched their group's rule.
- The attribute syncer's audit entries go to CloudWatch in full while S3 is down, as the requester's and revoker's do; they were lost before. They now hold `request_source` `attribute_sync`.
- Group entries and revoker entries record `request_source`, so a filter on `slack` or `cli` no longer drops them. A scheduled revocation records the source of the request it ends, and a sweep removal records `revoker`.
- The attribute syncer's `maximum_retry_attempts = 0` now applies; before, it ran with Lambda's default of 2 async retries after a function error. The apply creates one `aws_lambda_function_event_invoke_config` on the syncer, which replaces any async invoke settings configured on that function outside Terraform. The revoker keeps the default retries on purpose.

## Older versions

See [GitHub Releases](https://github.com/fivexl/terraform-aws-sso-elevator/releases).
