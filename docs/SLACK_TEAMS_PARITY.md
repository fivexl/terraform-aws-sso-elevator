# Mapping: Slack → Microsoft Teams (SSO Elevator)

One **domain** layer (rules, AWS) can be shared; **transport and UI** differ. Below: **what** lives where, **how** similar things are, and **what Teams alternatives** exist for each Slack interaction.

---

## 1. Architecture overview

### Slack (current)

```
User → Slack Client → Slack Platform (HTTP POST) → Lambda (Slack Bolt) → AWS SSO / EventBridge
                                                  ← Slack Web API (chat.postMessage, views.open, etc.)
```

### Teams (implemented)

```
User → Teams Client → Azure Bot Service (HTTP POST) → Lambda (Microsoft Teams SDK for Python) → AWS SSO / EventBridge
                                                     ← Teams / Bot Connector REST (via SDK: send/update activities)
                                                     ← Microsoft Graph API (user lookup by email; optional conversation members)
```

Key difference: Slack uses **one HTTPS endpoint + Bolt SDK**. Teams uses **Azure Bot registration + Microsoft Teams SDK for Python** (`microsoft-teams-apps`, `microsoft-teams-api` — see [teams.py](https://github.com/microsoft/teams.py)). The Lambda entry maps API Gateway events to `App.server.handle_request` in `src/requester/teams/teams_runtime.py`. Both can run on AWS Lambda.

---

## 2. Detailed action-by-action mapping

### 2.1 Entry points (shortcuts → bot commands / messaging extensions)

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| Global shortcut `request_for_access` | `app.shortcut("request_for_access")` in `main.py` | **Bot command** `/request-access` (or text `request access` / `request-access`) → **Adaptive Card** with a button that opens the form |
| Global shortcut `request_for_group_membership` | `app.shortcut("request_for_group_membership")` in `main.py` | **Bot command** `/request-group` (or `request group` / `request-group`) → same pattern |

**Teams details (parity with Slack “open modal”):**
- There is no Teams API equivalent to Slack’s `views.open(trigger_id, …)` in direct response to a **plain `message`** activity: the client does **not** open a task module from the JSON body of that HTTP response.
- The supported pattern is: reply to the message with an **Adaptive Card** whose `Action.Submit` includes `"msteams": { "type": "task/fetch" }` and your payload (e.g. `kind: account` | `group`). That triggers an **`invoke` / `task/fetch`** to the bot; the bot’s HTTP response to **that** invoke may return `task.type: continue` with the form card — this is when Teams shows the dialog ([Use dialogs with bots](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/task-modules/task-modules-bots)).
- Optional alternatives where it fits your rollout: **messaging extension** (`composeExtension/fetchTask`), **static tab** with a form, or a **pinned card** in a channel tab with the same `task/fetch` button.

**Implementation (SSO Elevator):** `src/requester/teams/teams_handlers.py` — `@app.on_message` validates the user in IAM Identity Center, then posts `teams_cards.build_request_access_launcher_card` (button → `task/fetch`). `@app.on_dialog_open` (`task/fetch`) loads accounts/permission sets or groups and returns the task module via `TaskModuleResponse` / `TaskModuleContinueResponse`. Submit is `@app.on_dialog_submit`. Approvals use `@app.on_card_action`. Lambda entry: `src/requester/teams/teams_runtime.py` → `process_teams_lambda_event`.

---

### 2.2 Modal forms (views.open / views.update → Task Modules / Adaptive Cards)

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `client.views_open(trigger_id, view)` | `main.py:show_initial_form_for_request` | **Two steps in Teams:** (1) `on_message` posts a launcher Adaptive Card; (2) user clicks **Open … form** → `task/fetch` → `on_dialog_open` returns the same form content inside `TaskModuleResponse` (continue with embedded Adaptive Card) |
| `client.views_update(view_id, view)` | `main.py:load_select_options_for_account_access_request` | `task/submit` → return updated Adaptive Card, or **typeahead search** in `Input.ChoiceSet` |
| `View(type="modal", callback_id, blocks=[...])` | `slack_helpers.py:RequestForAccessView.build()` | `TaskModuleResponse` with `AdaptiveCard` body |
| View submit (`view_submission`) | `app.view(CALLBACK_ID)` | `task/submit` handler in bot |

**Slack modal fields → Adaptive Card equivalents:**

| Slack Block Kit | Adaptive Card |
|-----------------|---------------|
| `StaticSelectElement` (account, permission set, group) | `Input.ChoiceSet` with `style: "filtered"` for search |
| `PlainTextInputElement` (reason, multiline) | `Input.Text` with `isMultiline: true` |
| `SectionBlock` + `MarkdownTextObject` | `TextBlock` with markdown or `RichTextBlock` |
| `DividerBlock` | `Container` with separator or `ColumnSet` |
| `InputBlock` with label | `Input.*` with `label` property |
| Loading placeholder (`":hourglass: Loading..."`) | `ProgressBar` or text placeholder, then **card replacement** |

**Key differences:**
- Slack: `trigger_id` required to open modal (valid 3 seconds). Teams: the dialog is opened only after a **`task/fetch` invoke** (typically from a card button with `msteams: { "type": "task/fetch" }`). A bot cannot rely on returning a task payload in the **first** HTTP response to a user’s chat message.
- **SSO Elevator** therefore sends a **launcher card** from `on_message`, then builds the real form in `on_dialog_open` when `task/fetch` arrives — no `trigger_id` concept, but an extra user click compared to Slack’s one-shot shortcut.
- Slack: `view_id` needed to update modal. Teams: return new card from `task/submit` handler — no view_id tracking needed
- Slack: max 100 options in `StaticSelectElement`. Teams: `Input.ChoiceSet` with `style: "filtered"` supports **dynamic typeahead** via `Data.Query` (no hard limit)
- The `user_view_map` pattern in `main.py` is **not needed** in Teams — task module state is managed by the framework

**Lazy-loaded options pattern:**

Slack (current):
1. Open modal with loading placeholder
2. Lazy handler fetches accounts/permission sets
3. `views.update` replaces placeholder with selects

Teams (equivalent):
1. Return initial Adaptive Card with loading text from `task/fetch`
2. Use `Input.ChoiceSet` with `choices.data.type: "Data.Query"` for **dynamic search** — options loaded on-demand as user types
3. Or: fetch data before returning the card (if fast enough, <5s)

---

### 2.3 Channel messages (chat.postMessage → Send Activity)

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `client.chat_postMessage(channel, text, blocks)` | Used 15+ times across `main.py`, `group.py`, `revoker.py` | `ActivityContext` / `TeamsNotifier` + `MessageActivityInput` (or helpers in `teams_activity_helpers.py`) |
| `client.chat_postMessage(thread_ts=ts)` | Thread replies | `reply_to_id` on activity / `TeamsNotifier.send_thread_reply` |
| `client.chat_update(channel, ts, blocks, text)` | Update message (color coding, remove buttons) | `TeamsNotifier.update_message` or `ctx.api.conversations.activities(...).update` |

**Message structure mapping:**

| Slack blocks | Adaptive Card elements |
|-------------|----------------------|
| `HeaderSectionBlock` (emoji + text) | `TextBlock` with `size: "large"`, `weight: "bolder"`, color via `color` property |
| `SectionBlock(fields=[...])` | `FactSet` with `Fact` items (key-value pairs) |
| `ActionsBlock` with `ButtonElement` | `ActionSet` with `Action.Submit` buttons |
| `MarkdownTextObject` | `TextBlock` with markdown support |
| Color coding via emoji (🟢🟡🔴⚪) | `Container` with `style: "good"/"warning"/"attention"/"default"` or colored `TextBlock` |

**Approval request message (Adaptive Card equivalent):**
```json
{
  "type": "AdaptiveCard",
  "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
  "version": "1.5",
  "body": [
    {
      "type": "TextBlock",
      "text": "🟡 | AWS account access request | 🟡",
      "size": "large",
      "weight": "bolder"
    },
    {
      "type": "FactSet",
      "facts": [
        { "title": "Requester", "value": "<at>John Doe</at>" },
        { "title": "Account", "value": "production #123456789" },
        { "title": "Role name", "value": "AdministratorAccess" },
        { "title": "Reason", "value": "Deploy hotfix" },
        { "title": "Permission duration", "value": "2h 0m" }
      ]
    }
  ],
  "actions": [
    {
      "type": "Action.Submit",
      "title": "Approve",
      "style": "positive",
      "data": { "action": "approve", "request_id": "..." }
    },
    {
      "type": "Action.Submit",
      "title": "Discard",
      "style": "destructive",
      "data": { "action": "discard", "request_id": "..." }
    }
  ]
}
```

---

### 2.4 Approve / Discard buttons (action handlers → invoke handlers)

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `app.action("approve")` / `app.action("discard")` | `main.py:handle_button_click` | `on_invoke_activity` with `invoke.name == "adaptiveCard/action"` |
| `ButtonClickedPayload` parsed from message blocks text | `slack_helpers.py:ButtonClickedPayload` | `Action.Submit` `data` field — structured JSON, **no text parsing needed** |
| `ack()` + `lazy=[handle_button_click]` | Fast ack, heavy work in lazy | Return `InvokeResponse(status=200)` immediately, process in background |

**Critical improvement in Teams:**
- Slack: `ButtonClickedPayload` parses requester, account, role from **message block text** using string splitting (`field["text"].split(": ")[1]`). This is fragile.
- Teams: `Action.Submit` carries a `data` object with **structured JSON**. Put `request_id`, `requester_id`, `account_id`, `permission_set_name`, `group_id`, `reason`, `permission_duration` directly in `data`. No text parsing needed.

**Removing buttons after click:**

Slack:
```python
blocks = remove_blocks(message["blocks"], block_ids=["buttons"])
blocks.append(button_click_info_block(action, approver.id))
client.chat_update(channel, ts, blocks, text)
```

Teams:
```python
# Replace card with a new version without action buttons
updated_card = build_card_without_buttons(original_data, approver_name, action)
await turn_context.update_activity(Activity(
    id=activity_id,
    type="message",
    attachments=[CardFactory.adaptive_card(updated_card)]
))
```

---

### 2.5 User identity resolution (Slack API → Microsoft Graph)

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `client.users_info(user=id)` → email, real_name | `slack_helpers.py:get_user` | `activity` sender + `ctx.api.conversations.members(...).get(id)` when available, else Graph by email in `teams_users.py` |
| `client.users_lookupByEmail(email=email)` | `slack_helpers.py:get_user_by_email` | Graph `GET /users?$filter=mail eq '{email}'` or `GET /users/{email}` |
| `entities.slack.User(id, email, real_name)` | `entities/slack.py` | `entities.teams.User(id, aad_object_id, email, display_name)` |

**Key differences:**
- Slack: email is in `user.profile.email`, always available. Teams: email is in **Entra ID (AAD)** via Microsoft Graph. Requires `User.Read.All` permission.
- Slack: rate limit = retry after 3s. Graph: rate limit = `Retry-After` header, typically 429 with backoff.
- Slack: one API call. Graph: may need **two** calls (get AAD ID from Teams context, then get email from Graph).
- Teams provides `aad_object_id` directly in the activity — this can be used to query Graph without email lookup.

**Implementation:** see `get_user_from_activity` and `get_user_by_email` in `src/teams_users.py` (conversation members API when the SDK exposes it; Graph for email lookup and revoker resolution).

---

### 2.6 Mentions (pings)

| Slack | Teams |
|-------|-------|
| `<@U12345>` in text | `<at>Display Name</at>` in text + `entities` array with `Mention` objects |

**Teams mention format:**
```python
mention = Mention(
    mentioned=ChannelAccount(id=user_id, name=display_name),
    text=f"<at>{display_name}</at>",
    type="mention"
)
activity = Activity(
    type="message",
    text=f"<at>{display_name}</at> there is a request waiting for approval.",
    entities=[mention]
)
```

---

### 2.7 DMs (Direct Messages → Proactive 1:1 messages)

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `client.chat_postMessage(channel=user_id, text=dm_text)` | `main.py`, `group.py` — conditional DM | Proactive messaging via `ConversationReference` |
| `check_if_user_is_in_channel` → send DM if not | `slack_helpers.py:check_if_user_is_in_channel` | Check team roster via `TeamsInfo.get_team_members` |

**Key differences:**
- Slack: DM is just `chat.postMessage(channel=user_id)`. Simple.
- Teams: **proactive messaging** uses a `ConversationReference`–compatible structure and the SDK `activity_sender` (see `src/teams_notifier.py`); not the legacy `BotFrameworkAdapter.continue_conversation` call path.
- Teams: org policies may **block** proactive 1:1 messages from bots. Need to handle `403 Forbidden`.
- Teams: bot must be **installed** for the user (personal scope) to send proactive messages.

**Implementation:** `TeamsNotifier.send_proactive_dm` in `src/teams_notifier.py` (initialized with shared `get_teams_app()` from `teams_runtime`).

---

### 2.8 Threading (thread_ts → reply chains)

| Slack | Teams |
|-------|-------|
| `chat_postMessage(thread_ts=ts)` | `send_activity` with `conversation.id` pointing to the reply chain |
| `message["ts"]` as thread identifier | `activity.id` as reply-to identifier |

Teams threading works via **reply chains** in channels. When posting a reply:
```python
await turn_context.send_activity(Activity(
    type="message",
    text="Request still awaiting approval...",
    conversation=ConversationReference(id=original_conversation_id),
    # reply_to_id is set automatically when replying in context
))
```

---

### 2.9 Message updates (chat.update → update_activity)

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `client.chat_update(channel, ts, blocks, text)` | Color coding updates, button removal | `TeamsNotifier.update_message` or conversations activities API |
| Identify message by `channel + ts` | Throughout codebase | Identify by `activity.id` + `conversation.id` |

**Used for:**
1. **Color coding** — change header emoji after decision → change `Container.style` or `TextBlock.color`
2. **Remove buttons** — strip `ActionsBlock` after approve/discard → send updated card without `ActionSet`
3. **Add footer** — append "pressed approve/discard" info → add `TextBlock` to card body
4. **Expiration** — remove buttons + add "expired" text → same as above

---

### 2.10 Scheduled events & notifications

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `schedule_discard_buttons_event` | `schedule.py` → EventBridge → revoker Lambda | **Same** EventBridge mechanism, revoker calls Teams Bot API instead of Slack API |
| `schedule_approver_notification_event` | `schedule.py` → EventBridge → revoker Lambda | **Same** — post thread reply via Bot Framework |
| `handle_discard_buttons_event` | `revoker.py` — `chat.update` to remove buttons | `update_activity` with card without buttons |
| `handle_approvers_renotification_event` | `revoker.py` — `chat.postMessage` in thread | `TeamsNotifier.send_thread_reply` |

**No change needed** in EventBridge Scheduler logic. Only the **notification delivery** changes: instead of `slack_client.chat_postMessage`, use `TeamsNotifier` (Teams SDK on top of the same app credentials as `main`).

The revoker Lambda uses `configure_teams_dependencies` + `get_teams_app` so it shares the same initialized Teams `App` pattern as the requester handler.

---

### 2.11 Signature verification & security

| Slack | Teams |
|-------|-------|
| `X-Slack-Signature` + HMAC-SHA256 | JWT token validation (Azure Bot Service) |
| Slack Bolt handles automatically | Teams `App` / `handle_request` validates Bot Framework service auth |
| One secret (`SLACK_SIGNING_SECRET`) | App ID + App Password (or Managed Identity) |

Teams authentication:
- Inbound: Bot Framework SDK validates JWT from Azure Bot Service
- Outbound: SDK authenticates with `MicrosoftAppId` + `MicrosoftAppPassword`
- Optional: **Managed Identity** for passwordless auth in Azure

---

### 2.12 Ack + lazy pattern (3s timeout → invoke response + background)

| Slack | Teams |
|-------|-------|
| `ack()` within 3 seconds | Return `InvokeResponse(status=200)` within ~5-15 seconds |
| `lazy=[handler]` for heavy work | Background processing (SQS queue, or `asyncio.create_task`) |
| Bolt `process_before_response=True` | No direct equivalent; SDK processes sequentially |

**Teams timeout handling:**
- `task/submit` and `adaptiveCard/action` invokes have **~15 second** timeout (varies by scenario)
- For long-running AWS operations: return immediate response, process in background, then **update the card** with results
- Pattern: return "Processing..." card → do AWS work → `update_activity` with final result

---

### 2.13 Error handling & user notifications

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `error_handler` → `chat.postMessage` with error text | `errors.py` | `send_activity` with error text or error Adaptive Card |
| `<@{user_id}>` mention in error | `errors.py:error_handler` | `<at>User</at>` mention in error card |
| `SSOUserNotFound` specific message | `errors.py` | Same message, different delivery |

---

### 2.14 Rate limiting & resilience

| Slack | Code | Teams equivalent |
|-------|------|------------------|
| `SlackApiError` with `ratelimited` → sleep 3s + retry | `slack_helpers.py:get_user_by_email` | Graph API `429` → respect `Retry-After` header |
| 30s timeout for retries | `slack_helpers.py` | Similar timeout, but Graph rate limits are **per-tenant** |
| Duplicate request cache (in-memory) | `main.py`, `group.py` | Same pattern works; consider DynamoDB for multi-instance |

---

### 2.15 Configuration changes needed

| Current (Slack) | New (Teams) | Notes |
|-----------------|-------------|-------|
| `slack_bot_token` | `microsoft_app_id` + `microsoft_app_password` | Bot Framework credentials |
| `slack_channel_id` | `teams_channel_id` + `teams_team_id` | Teams channel identification requires both |
| `slack_app_log_level` | `bot_log_level` | Same concept |
| `send_dm_if_user_not_in_channel` | `send_dm_if_user_not_in_channel` | Same flag, different implementation |
| `post_update_to_slack` | `post_update_to_teams` | Same flag |
| Emoji config (`good_result_emoji`, etc.) | Color/style config (`good_result_style`, etc.) | Adaptive Card uses `style` not emoji |

---

### 2.16 Events model (`entities.slack.User` in events)

| Current | Impact | Teams equivalent |
|---------|--------|------------------|
| `RevokeEvent.approver: entities.slack.User` | Stored in EventBridge schedule payload | `entities.teams.User` or generic `entities.User` |
| `RevokeEvent.requester: entities.slack.User` | Same | Same |
| `s3.AuditEntry.requester_slack_id` | S3 audit logs | `requester_teams_id` or generic `requester_platform_id` |
| `s3.AuditEntry.approver_slack_id` | S3 audit logs | `approver_teams_id` or generic `approver_platform_id` |

**Recommendation:** abstract `entities.slack.User` into a generic `entities.User` with `platform: "slack" | "teams"` field. This allows both platforms to coexist.

---

## 3. What is easier / harder in Teams

### Easier in Teams
| Topic | Why |
|-------|-----|
| **Structured button data** | `Action.Submit.data` carries JSON — no need to parse text from message blocks |
| **Dynamic search in selects** | `Input.ChoiceSet` with `Data.Query` supports typeahead with no 100-option limit |
| **No view_id tracking** | Task modules are stateless — no `user_view_map` needed |
| **User identity** | AAD Object ID available directly in activity — no separate API call for basic info |
| **Card versioning** | Adaptive Cards have explicit versioning and fallback |

### Harder in Teams
| Topic | Why |
|-------|-----|
| **Proactive DMs** | Requires stored `ConversationReference` + bot installed for user + org policy allows it |
| **Global entry point** | No "shortcut everywhere" — need bot command, messaging extension, or tab |
| **Deployment** | Bot registration in Azure + Teams app manifest + admin approval for org-wide install |
| **Multi-tenant** | Per-Entra-tenant; cross-tenant is complex |
| **Message history lookup** | No direct `conversations.history` equivalent — need to store `activity.id` for later updates |
| **Rate limits** | Graph API rate limits are per-tenant, more complex throttling model |

### Different but equivalent
| Topic | Notes |
|-------|-------|
| **Auth** | HMAC → JWT; both handled by SDK |
| **Timeout** | 3s → 5-15s; both need async pattern for heavy work |
| **Threading** | `thread_ts` → reply chain; semantically identical |
| **Mentions** | `<@U...>` → `<at>Name</at>` + entities array |

---

## 4. Implementation strategy

### Phase 1: Abstraction layer
1. Extract `entities.slack.User` → generic `entities.User(id, email, display_name, platform)`
2. Create `MessagingClient` interface with methods: `send_message`, `update_message`, `send_dm`, `get_user`, `get_user_by_email`, `check_user_in_channel`, `open_form`, `update_form`
3. Implement `SlackMessagingClient` (wraps current `WebClient` calls) and `TeamsMessagingClient` (wraps Teams SDK / `TeamsNotifier` where a shared abstraction is used)

### Phase 2: Teams app (as implemented)
1. `src/teams_runtime.py` — `App` construction, `process_teams_lambda_event` for API Gateway
2. `src/teams_handlers.py` — command handlers (`/request-access`, `/request-group`), dialog and card action routes
3. `src/teams_notifier.py` — channel posts, updates, thread replies, proactive DMs
4. `src/teams_cards.py` — Adaptive Card JSON

### Phase 3: Configuration
1. Add `messaging_platform: "slack" | "teams"` config parameter
2. Add Teams-specific config (`microsoft_app_id`, `microsoft_app_password`, `teams_channel_id`, `teams_team_id`)
3. Update Terraform to optionally deploy Teams bot infrastructure

### Phase 4: Revoker updates
1. Update `revoker.py` to use `MessagingClient` interface
2. Both Slack and Teams notifications go through the same abstraction
3. EventBridge events remain unchanged; only delivery changes

---

## 5. Dependencies

### Current (Slack)
- `slack-bolt` — Slack Bolt framework
- `slack-sdk` — Slack Web API client

### New (Teams)
- `microsoft-teams-apps` / `microsoft-teams-api` — Microsoft Teams SDK for Python ([teams.py](https://github.com/microsoft/teams.py))
- `msgraph-sdk` — Microsoft Graph client (user lookup by email; `teams_users.py`)
- `azure-identity` — used via Graph / Kiota where applicable

---

## 6. Summary

| Aspect | Slack | Teams | Effort |
|--------|-------|-------|--------|
| Entry points | Shortcuts | Bot commands / ME | Medium |
| Forms | Block Kit modals | Adaptive Card task modules | Medium |
| Messages | Block Kit | Adaptive Cards | Medium |
| Buttons | Action blocks | Action.Submit | Low (better in Teams) |
| User lookup | Slack API | Microsoft Graph | Medium |
| DMs | Simple | Proactive messaging | High |
| Threading | thread_ts | Reply chains | Low |
| Message updates | chat.update | update_activity | Low |
| Auth | HMAC | JWT (Bot connector; Teams SDK handles) | Low |
| Scheduling | EventBridge (unchanged) | EventBridge (unchanged) | None |
| Business logic | Unchanged | Unchanged | None |

**Business logic** and **AWS integrations** need **not** be duplicated. The split is: **Slack driver** / **Teams driver** → shared **domain layer** (access_control, sso, schedule, s3). UI and transport move (Block Kit → Adaptive Card, Bolt → Microsoft Teams SDK for Python) and do **not** match line for line, but every Slack capability has a Teams equivalent or acceptable alternative.
