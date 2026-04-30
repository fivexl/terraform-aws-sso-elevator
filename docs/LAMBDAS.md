# Lambda functions in SSO Elevator

All functions are built from a single `src/` directory (see `patterns` in Terraform), share a common dependency layer (except when using a pre-built image) and differ by **handler** (Python module and function) and environment variables / IAM.

| Terraform name | Terraform file | Handler (Zip) | Main module |
|----------------|------------------|---------------|-------------|
| `access_requester_slack_handler` | `slack_handler_lambda.tf` | `main.lambda_handler` | `src/main.py` |
| `access_revoker` | `perm_revoker_lambda.tf` | `revoker.lambda_handler` | `src/revoker.py` |
| `attribute_syncer` (optional) | `attribute_syncer_lambda.tf` | `attribute_syncer.lambda_handler` | `src/attribute_syncer.py` |

With `use_pre_created_image`, the artifact is **different** ECR tags (`requester-*`, `revoker-*`, `attribute-syncer-*`); entrypoint is defined in the image.

---

## 1. Requester (Slack or Teams)

**Purpose:** accept HTTP from the chat platform, drive request forms, approval cards, and grant / schedule access.

**Inputs:** API Gateway HTTP API → `main.lambda_handler` (see `slack_handler_lambda.tf`).

### Slack (`chat_platform = "slack"`)

`lambda_handler` delegates to `SlackRequestHandler`:

- Shortcuts `request_for_access` and `request_for_group_membership` — modals with accounts / permission sets or groups.
- View submission — access / group requests; logic in `access_control`, `group`, `schedule`, `sso`, `s3`.
- Approve / Discard buttons — payload parsing, `access_control`, and `execute_decision`.

### Teams (`chat_platform = "teams"`)

`lambda_handler` runs `process_teams_lambda_event` → `microsoft_teams.apps.App` (`src/requester/teams/teams_runtime.py`).

- **Message** matching `/request-access` or `/request-group` (and the text variants in `teams_handlers`) — bot replies with a **launcher Adaptive Card**; the user clicks **Open … form** so Teams sends **`task/fetch`** (see `build_request_access_launcher_card` in `teams_cards.py`). The form itself is returned from **`on_dialog_open`**, not from the first message response (Teams platform requirement).
- **`task/submit`** — same domain logic as Slack view submission (`access_control`, `group`, `schedule`, …). The approval card is posted to the configured Teams conversation via an **asynchronous self-invoke** (`lambda:InvokeFunction`, same function) so the HTTP response to `task/submit` returns within Teams’ client timeout (~15s); the second invocation performs `TeamsNotifier.send_message` (Bot Framework outbound HTTPS). Ensure the Lambda has **egress to Microsoft** (e.g. NAT gateway in a private VPC; otherwise `httpx.ConnectError` / “Unable to reach app” in the client).
- **Adaptive Card actions** — approve / discard; same AWS outcome as Slack.

Related modules: `access_control.py`, `group.py`, `slack_helpers.py` (shared user shape), `requester/teams/*`, `schedule.py`, `sso.py`, `organizations.py`, `s3.py`, `config.py`.

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
