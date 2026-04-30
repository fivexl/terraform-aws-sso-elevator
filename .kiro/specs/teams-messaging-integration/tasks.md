# Implementation Tasks: Microsoft Teams Integration for SSO Elevator

## Tasks

- [x] 1. Clean up dead code
  - [x] 1.1 Delete `src/ports.py`
  - [x] 1.2 Delete `src/teams_adapter.py`

- [x] 2. Add `src/entities/teams.py` — TeamsUser entity
  - [x] 2.1 Create `TeamsUser` Pydantic model with `id`, `aad_object_id`, `email`, `display_name` fields
  - [x] 2.2 Add `real_name` property returning `display_name`
  - [x] 2.3 Add `to_slack_user()` method returning `entities.slack.User`
  - [x] 2.4 Export `TeamsUser` from `src/entities/__init__.py`

- [x] 3. Add `src/teams_cards.py` — Adaptive Card builders
  - [x] 3.1 Implement `build_account_access_form(accounts, permission_sets, duration_options) -> dict`
  - [x] 3.2 Implement `build_group_access_form(groups, duration_options) -> dict`
  - [x] 3.3 Implement `build_approval_card(requester_name, account, group, role_name, reason, permission_duration, show_buttons, color_style, request_data, elevator_request_id) -> dict`
  - [x] 3.4 Implement `update_card_after_decision(original_card, decision_action, approver_name, color_style) -> dict`
  - [x] 3.5 Implement `update_card_on_expiry(original_card, expiration_hours, expired_style) -> dict`
  - [x] 3.6 Implement `get_color_style(emoji_config: str) -> str` mapping Slack emoji config values to Adaptive Card container styles

- [x] 4. Add `src/teams_users.py` — Teams user resolution
  - [x] 4.1 Implement `async get_user_from_activity(turn_context) -> entities.teams.TeamsUser`
  - [x] 4.2 Implement `async get_user_by_email(graph_client, email) -> entities.teams.TeamsUser` with 429 retry (max 3 retries, respecting Retry-After header)
  - [x] 4.3 Implement `async check_user_in_channel(turn_context, channel_id, user_aad_id) -> bool`
  - [x] 4.4 Implement `build_mention(user_id, display_name) -> tuple[str, dict]` returning mention text and Mention entity object
  - [x] 4.5 Implement `async resolve_principal_to_teams_user(graph_client, sso_user_id, sso_client, identity_store_client, cfg) -> entities.teams.TeamsUser | None`

- [x] 5. Extend `src/events.py` — add optional Teams fields
  - [x] 5.1 Add `teams_conversation_id: str | None = None` and `teams_activity_id: str | None = None` to `DiscardButtonsEvent`
  - [x] 5.2 Add `teams_conversation_id: str | None = None` and `teams_activity_id: str | None = None` to `ApproverNotificationEvent`

- [x] 6. Extend `src/request_store.py` — Teams state persistence
  - [x] 6.1 Implement `update_teams_presentation(elevator_request_id, conversation_id, activity_id) -> None` (stores `teams_conversation_id` and `teams_activity_id` on the ACCESS_REQUEST item)
  - [x] 6.2 Implement `save_conversation_reference(user_aad_id, reference: dict) -> None` (stores a `CONV_REF` entity with key `convref:{user_aad_id}`)
  - [x] 6.3 Implement `get_conversation_reference(user_aad_id) -> dict | None`

- [x] 7. Extend `src/config.py` — Teams platform validation
  - [x] 7.1 Add `model_validator(mode="after")` named `validate_platform_config` that raises `ValueError` listing missing fields when `chat_platform == "teams"` and any of `teams_microsoft_app_id`, `teams_microsoft_app_password`, `teams_azure_tenant_id`, `teams_approval_conversation_id` are empty

- [x] 8. Add `TeamsNotifier` class to `src/revoker.py`
  - [x] 8.1 Implement `TeamsNotifier.__init__(self, cfg)` storing app credentials
  - [x] 8.2 Implement `async TeamsNotifier.send_message(text, card=None) -> str` — posts to approval channel, returns activity_id
  - [x] 8.3 Implement `async TeamsNotifier.update_message(activity_id, card) -> None` — updates existing card by activity_id
  - [x] 8.4 Implement `async TeamsNotifier.send_thread_reply(parent_activity_id, text) -> None`
  - [x] 8.5 Implement `async TeamsNotifier.send_proactive_dm(conversation_reference, text) -> None`
  - [x] 8.6 Add `get_notifier(cfg) -> slack_sdk.WebClient | TeamsNotifier` factory function

- [x] 9. Update `src/revoker.py` — platform-aware event handling
  - [x] 9.1 Replace the module-level `slack_client = slack_sdk.WebClient(...)` with a call to `get_notifier(cfg)` so the revoker uses Teams when `chat_platform == "teams"`
  - [x] 9.2 Update `handle_discard_buttons_event` to branch on `cfg.chat_platform`: for Teams, call `TeamsNotifier.update_message` using `event.teams_conversation_id` / `event.teams_activity_id`; for Slack, keep existing logic
  - [x] 9.3 Update `handle_approvers_renotification_event` to branch on `cfg.chat_platform`: for Teams, call `TeamsNotifier.send_thread_reply`; for Slack, keep existing logic
  - [x] 9.4 Update `handle_scheduled_account_assignment_deletion` and `handle_scheduled_group_assignment_deletion` to use `TeamsNotifier` notification methods when `chat_platform == "teams"`

- [x] 10. Add Teams bot and handler to `src/main.py`
  - [x] 10.1 Add `SSOElevatorBot(ActivityHandler)` class with `on_message_activity` routing `/access` and `/group-access` commands
  - [x] 10.2 Add `on_invoke_activity` dispatching `task/fetch`, `task/submit`, and `adaptiveCard/action` invoke types
  - [x] 10.3 Implement `_handle_task_fetch` — returns `TaskModuleResponse` with account or group access form card
  - [x] 10.4 Implement `_handle_task_submit` — parses form data, calls `access_control.make_decision_on_access_request`, stores request via `request_store.put_access_request`, posts approval card to channel, schedules EventBridge events
  - [x] 10.5 Implement `_handle_card_action` — reads `elevator_request_id` from `Action.Submit.data`, calls `request_store.try_begin_in_flight_approval`, calls `access_control.make_decision_on_approve_request`, updates card via `update_activity`, calls `access_control.execute_decision`
  - [x] 10.6 Implement `async handle_teams_event(event, context) -> dict` — converts API Gateway event body to Bot Framework activity and processes it through `SSOElevatorBot`
  - [x] 10.7 Update `lambda_handler` to route: `if cfg.chat_platform == "teams": return asyncio.run(handle_teams_event(event, context))` else existing Slack Bolt path

- [x] 11. Add Teams group access handler to `src/group.py`
  - [x] 11.1 Implement `async handle_teams_group_task_submit(turn_context, data, cfg, ...) -> TaskModuleResponse` — parses group form submission, calls `access_control.make_decision_on_access_request` with `group_statements`, stores request, posts approval card, schedules EventBridge events
  - [x] 11.2 Implement `async handle_teams_group_card_action(turn_context, data, cfg, ...) -> InvokeResponse` — handles Approve/Discard button clicks for group requests, mirrors `handle_group_button_click` logic

- [x] 12. Add new Python dependencies to `src/requirements.txt`
  - [x] 12.1 Add `botbuilder-core>=4.14.0`
  - [x] 12.2 Add `botbuilder-schema>=4.14.0`
  - [x] 12.3 Add `botbuilder-integration-aiohttp>=4.14.0`
  - [x] 12.4 Add `msgraph-sdk>=1.0.0`
  - [x] 12.5 Add `azure-identity>=1.15.0`

- [x] 13. Write property-based tests for `teams_cards.py` and `entities/teams.py`
  - [x] 13.1 Write Property 1 test: form card completeness — for any non-empty accounts/permission-sets/groups list, `build_account_access_form` and `build_group_access_form` produce cards with the correct `Input.ChoiceSet` counts
    **Validates: Requirements 2.1, 2.2**
  - [x] 13.2 Write Property 2 test: form submission parsing round-trip — for any valid account ID, permission set, duration, reason, and requester ID embedded in a task/submit payload, parsing produces a request object with matching field values
    **Validates: Requirements 2.4**
  - [x] 13.3 Write Property 3 test: approval card completeness — for any requester name, account/group, role, reason, and duration, `build_approval_card` produces a card whose FactSet contains every required field with the correct value
    **Validates: Requirements 3.1, 3.2**
  - [x] 13.4 Write Property 4 test: buttons match approval requirement — when `show_buttons=True` the card has exactly two `Action.Submit` buttons (Approve positive, Discard destructive) with `elevator_request_id` in data; when `show_buttons=False` no ActionSet is present
    **Validates: Requirements 3.3, 3.4**
  - [x] 13.5 Write Property 5 test: card color style application — for any valid color style string (`good`, `warning`, `attention`, `default`), building or updating a card sets the header Container `style` to that exact value
    **Validates: Requirements 3.5, 4.4**
  - [x] 13.6 Write Property 6 test: mention formatting — for any non-empty display name and user ID, `build_mention` produces text containing `<at>{display_name}</at>` and a Mention entity with `mentioned.id` equal to the user ID
    **Validates: Requirements 3.6, 12.1**
  - [x] 13.7 Write Property 7 test: card state transition preserves content — for any approval card with an ActionSet and FactSet, applying `update_card_after_decision` or `update_card_on_expiry` produces a card where the FactSet is unchanged, no ActionSet is present, and a new TextBlock with decision/expiry info is appended
    **Validates: Requirements 4.3, 10.2**
  - [x] 13.8 Write Property 8 test: TeamsUser to Slack User compatibility — for any valid `TeamsUser`, `to_slack_user()` produces a `slack.User` where `id`, `email`, and `real_name` match the TeamsUser fields
    **Validates: Requirements 5.4**
  - [x] 13.9 Write Property 9 test: audit log completeness — for any Teams request with non-empty requester and approver data, the resulting `AuditEntry` has non-empty `requester_email`, `approver_email`, and non-"NA" `requester_slack_id` / `approver_slack_id` fields
    **Validates: Requirements 13.1, 13.2**

- [x] 14. Write unit tests for Teams integration
  - [x] 14.1 Test `get_color_style` maps all four Slack emoji config values to the correct Adaptive Card container styles
  - [x] 14.2 Test `config.py` validation: `chat_platform=teams` with missing required params raises `ValueError` listing the missing fields; with all params present no error is raised
  - [x] 14.3 Test `request_store` Teams extensions: `update_teams_presentation`, `save_conversation_reference`, and `get_conversation_reference` round-trip correctly using the in-memory store
  - [x] 14.4 Test `DiscardButtonsEvent` and `ApproverNotificationEvent` accept and round-trip the new optional `teams_conversation_id` and `teams_activity_id` fields without breaking existing Slack-only payloads
  - [x] 14.5 Test `TeamsUser.to_slack_user()` produces a `slack.User` with the correct field values
  - [x] 14.6 Test `build_mention` returns the correct `<at>` text and Mention entity dict structure
