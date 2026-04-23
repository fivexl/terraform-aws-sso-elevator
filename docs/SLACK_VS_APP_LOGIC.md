# Capabilities and split of logic: Slack vs SSO Elevator code

In short: **Slack** renders UI, delivers events, and shows results **after** the API response. **Code** (Lambda + Slack Bolt) accepts web requests, validates the signature, applies business rules, calls **AWS** and the **Slack Web API** to update chats and modals.

---

## Terms

| Where | What it is |
|-------|------------|
| **Slack (platform)** | Clients, Block Kit, HTTP delivery, API limits, message rendering |
| **Application code** | Repo `src/`, Bolt handlers, `slack_helpers`, `access_control`, `group`, AWS integrations |

---

## Capability list

| Capability | Logic in **Slack** (platform) | Logic in **code** |
|------------|-------------------------------|-------------------|
| **Global Shortcuts** (`request_for_access`, `request_for_group_membership`) | Lightning / Shortcuts menu, opening the flow, passing `trigger_id` and `user` in the payload | Config checks, user validation in IAM Identity Center, `views.open`, storing `view_id` in `user_view_map` |
| **Modal** | Block Kit render: fields, selects, Submit/Cancel, collecting `view.state` on submit | Building `View` in `slack_helpers`, loading account/group/permission set lists, `views.update` / fallback `views.open` |
| **Modal submit (`view_submission`)** | Required-field checks at UI level, sending JSON form state | Field `parse`, `access_control` (approval rules), `chat.postMessage`, reminder scheduler (EventBridge Scheduler) |
| **Duration selector (`static_select`)** | Selected option changes in modal state | `duration_picker_action`: `ack()` only; value is read on **submit** in code |
| **Request message in channel (blocks, thread)** | Block display, `<@U…>` pings, threads via `thread_ts` | `build_approval_request_message_blocks`, post to `slack_channel_id`, header style updates (`chat.update`) |
| **Approve / Discard buttons** | Click → interactive payload with `user`, `message`, `action` | Parsing `ButtonClickedPayload` from block **text** `content`, `access_control.make_decision_on_approve_request`, `execute_decision` (AWS SSO, etc.) |
| **Removing buttons after a decision** | Message without `ActionsBlock` | `remove_blocks` / updating `message.blocks` via `chat.update` |
| **Approver reminders / auto-remove buttons** | New message display or scheduled **external** event if another Lambda | `schedule_*` plans in code; execution — **separate** Lambdas/rules (not the same as the request path; see `schedule.py` and Terraform) |
| **DM user if not in channel** | Delivery to bot DM, required OAuth scopes | `conversations_members` + `send_dm_if_user_not_in_channel`, `chat_postMessage` to `channel=user_id` |
| **Mapping Slack ↔ email ↔ SSO** | Slack profiles (`email` in `users.info`) | `get_user` / `users_lookupByEmail`, `sso.get_user_principal_id_by_email`, “secondary domain” in messages |
| **Endpoint protection** | (none) | `X-Slack-Signing-Secret` verification (Bolt), your code only |
| **Three-second deadline** | HTTP POST waits for response | `process_before_response` + `ack` + **lazy** listeners — fast 200, heavy work in lazy |

---

## What Slack does **not** do

- It does not know AWS Organizations, Identity Center, permission sets — only what **code** put in options or block text.
- It does not “approve” access: the button only sends an **event**; **decision** and SSO `Assign` are in **code** (`access_control`, `execute_decision`, etc.).
- It does not keep `view_id` across warm/cold Lambda starts — hence `user_view_map` and fallback when `view_id` is lost in code.

## What code does **not** do by itself

- It does not draw native Slack UI: it only supplies Block Kit / view **descriptions** via API.
- It does not push to the desktop: that is **Slack clients** and Slack infrastructure.

---

## One-sentence summary

**Slack** is transport and **storefront** (forms, chats, buttons). **Code** is **policy, AWS, and dispatch** of responses back to Slack via `WebClient`.
