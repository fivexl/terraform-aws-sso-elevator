# Lambda functions in SSO Elevator

All functions are built from a single `src/` directory (see `patterns` in Terraform), share a common dependency layer (except when using a pre-built image) and differ by **handler** (Python module and function) and environment variables / IAM.

| Terraform name | Terraform file | Handler (Zip) | Main module |
|----------------|------------------|---------------|-------------|
| `access_requester_slack_handler` | `slack_handler_lambda.tf` | `main.lambda_handler` | `src/main.py` |
| `access_revoker` | `perm_revoker_lambda.tf` | `revoker.lambda_handler` | `src/revoker.py` |
| `attribute_syncer` (optional) | `attribute_syncer_lambda.tf` | `attribute_syncer.lambda_handler` | `src/attribute_syncer.py` |

With `use_pre_created_image`, the artifact is **different** ECR tags (`requester-*`, `revoker-*`, `attribute-syncer-*`); entrypoint is defined in the image.

---

## 1. Requester (Slack)

**Purpose:** accept HTTP from Slack (Slack Bolt), handle shortcuts, modals, approval buttons, and grant / schedule access.

**Inputs:** Function URL and/or API Gateway HTTP API → same handler (see `slack_handler_lambda.tf`).

**Code:** `lambda_handler` delegates to `SlackRequestHandler`:

- Shortcuts `request_for_access` and `request_for_group_membership` — forms, lists of accounts / permission sets or groups.
- View submission — access / group requests; logic in `access_control`, `group`, `schedule`, `sso`, `s3`.
- Approve / Discard buttons — payload parsing, `access_control`, and `execute_decision`.

Related modules: `access_control.py`, `group.py`, `slack_helpers.py`, `schedule.py`, `sso.py`, `organizations.py`, `s3.py`, `config.py`.

---

## 2. Revoker (revocation)

**Purpose:** revoke temporary access, scheduler-driven flows, and alignment with events.

**Inputs:** mainly **EventBridge** (schedule `sso_elevator_scheduled_revocation`, separate rule `check_on_inconsistency`); plus payloads published by **EventBridge Scheduler** and related code (types in `src/events.py` — see `Event` / `*Event` in `revoker.py`).

**Code:** `revoker.lambda_handler` parses the event (Pydantic) and by `match` on type calls:

- Scheduled revocation of **account assignment** / **group membership**, S3 audit, optional Slack notification.
- `SSOElevatorScheduledRevocation` — compare “what is in SSO / Identity Store” vs “what is scheduled in Scheduler”; remove stray assignments.
- `CheckOnInconsistency` / group checks — detect mismatches and post to the channel.
- `DiscardButtonsEvent` — remove buttons when the request expires.
- `ApproverNotificationEvent` — reminders to approvers.

---

## 3. Attribute syncer (optional)

**Purpose:** on a schedule, sync **group memberships** in IAM Identity Center using “user attributes → group” rules (see `attribute_sync_*` variables in Terraform).

**Module creation:** `count` in `attribute_syncer_lambda.tf` — only when `attribute_sync_enabled`.

**Code:** `attribute_syncer.lambda_handler` loads config (`sync_config`), builds rules (`attribute_mapper`, `SyncStateManager`), applies add/remove/warn, writes audit to S3, sends summaries / errors to Slack.

---

## Quick navigation in the repo

- Slack handler Terraform: `slack_handler_lambda.tf`
- Revoker Terraform: `perm_revoker_lambda.tf`
- Attribute syncer Terraform: `attribute_syncer_lambda.tf`
- Source: `src/main.py`, `src/revoker.py`, `src/attribute_syncer.py`

For how Slack vs application code split responsibility, see [SLACK_VS_APP_LOGIC.md](SLACK_VS_APP_LOGIC.md).
