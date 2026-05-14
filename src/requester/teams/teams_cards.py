"""Adaptive Card builders for Microsoft Teams integration.

Pure functions that build Adaptive Card JSON structures.
Equivalent of the Block Kit building code in slack_helpers.py.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import access_control
    import entities.aws

import organizations

_DURATION_PARTS_HMS = 3
_DURATION_PARTS_HM = 2


def parse_duration_choice(duration_str: str) -> timedelta:
    """Parse duration from Teams Adaptive Card `duration` input (HH:MM, same as Slack, or HH:MM:SS)."""
    try:
        parts = duration_str.strip().split(":")
        if len(parts) == _DURATION_PARTS_HMS:
            return timedelta(hours=int(parts[0]), minutes=int(parts[1]), seconds=int(parts[2]))
        if len(parts) == _DURATION_PARTS_HM:
            return timedelta(hours=int(parts[0]), minutes=int(parts[1]))
    except (ValueError, TypeError, OverflowError):
        pass
    return timedelta(hours=1)


# Mapping from Slack emoji config values to Adaptive Card Container styles
_EMOJI_TO_STYLE: dict[str, str] = {
    ":large_green_circle:": "good",
    ":large_yellow_circle:": "warning",
    ":red_circle:": "attention",
    ":white_circle:": "default",
}


def get_color_style(emoji_config: str) -> str:
    """Map Slack emoji config values to Adaptive Card Container styles.

    Falls back to 'default' for unknown values.
    """
    return _EMOJI_TO_STYLE.get(emoji_config, "default")


def teams_access_request_card_style_and_subtitle(
    decision: "access_control.AccessRequestDecision",
    waiting_emoji: str,
    bad_emoji: str,
    *,
    is_group: bool = False,
) -> tuple[str, str | None]:
    """Adaptive Card container style and optional in-card subtitle.

    Auto-grant (self / approval not required): same neutral card as pending — status is posted in the thread
    (Slack parity), not inside the card.
    """
    import access_control as ac

    match decision.reason:
        case ac.DecisionReason.ApprovalNotRequired | ac.DecisionReason.SelfApproval:
            return get_color_style(waiting_emoji), None
        case ac.DecisionReason.RequiresApproval:
            return get_color_style(waiting_emoji), None
        case ac.DecisionReason.NoApprovers:
            return get_color_style(bad_emoji), "Nobody can approve this request."
        case ac.DecisionReason.NoStatements:
            return get_color_style(bad_emoji), (
                "There are no statements for this group." if is_group else "There are no statements for this permission set and account."
            )
        case _:
            return get_color_style(waiting_emoji), None


def teams_access_auto_grant_thread_status_text(
    decision: "access_control.AccessRequestDecision",
    *,
    is_group: bool = False,
) -> str | None:
    """Plain text for the first in-thread line when access was auto-granted (not shown on the card)."""
    import access_control as ac

    match decision.reason:
        case ac.DecisionReason.ApprovalNotRequired:
            return (
                "Approval for this group is not required. Your request was approved automatically."
                if is_group
                else "Approval for this permission set and account is not required. Your request was approved automatically."
            )
        case ac.DecisionReason.SelfApproval:
            return "Self approval is allowed and you are listed as an approver. Your request was approved automatically."
        case _:
            return None


def build_request_access_launcher_card(kind: str) -> dict:
    """Card after the user types a command: one button opens the task module (Slack global-shortcut modal analogue).

    Teams only opens dialogs in response to an ``invoke`` with ``task/fetch`` (e.g. from this card's
    ``Action.Submit``), not from the HTTP body of a reply to a plain ``message`` activity.
    """
    if kind == "group":
        title = "Request AWS group membership"
        blurb = (
            "Your user is recognized in IAM Identity Center. "
            "Use the button below to open the form and select a group, duration, and reason."
        )
        button_title = "Open group access form"
    else:
        title = "Request AWS account access"
        blurb = (
            "Your user is recognized in IAM Identity Center. "
            "Use the button below to open the form and select an account, permission set, duration, and reason."
        )
        button_title = "Open account access form"

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": title, "size": "large", "weight": "bolder", "wrap": True},
            {"type": "TextBlock", "text": blurb, "wrap": True},
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": button_title,
                "data": {
                    "msteams": {"type": "task/fetch"},
                    "kind": kind if kind in ("account", "group") else "account",
                },
            }
        ],
    }


def build_request_access_launcher_submitted_card(kind: str) -> dict:
    """After task/submit: same title as the launcher but without the open-form action (Slack: hide submit UX)."""
    if kind == "group":
        title = "Request AWS group membership"
        sub = "Form submitted. This thread has the approval card and status updates."
    else:
        title = "Request AWS account access"
        sub = "Form submitted. This thread has the approval card and status updates."
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": title, "size": "large", "weight": "bolder", "wrap": True},
            {"type": "TextBlock", "text": sub, "wrap": True},
        ],
    }


def build_account_access_form(
    accounts: list[entities.aws.Account],
    permission_sets: list[entities.aws.PermissionSet],
    duration_options: list[str],
    management_account_id: str | None = None,
) -> dict:
    """Build Adaptive Card for account access request Task Module."""
    account_choices: list[dict[str, str]] = [
        {
            "title": f"{a.name} (management account) ({a.id})"
            if organizations.is_management_account(a.id, management_account_id)
            else f"{a.name} ({a.id})",
            "value": a.id,
        }
        for a in accounts
    ]
    permission_set_choices = [{"title": ps.name, "value": ps.name} for ps in permission_sets]
    duration_choices = [{"title": d, "value": d} for d in duration_options]

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {
                "type": "TextBlock",
                "text": "Request AWS Account Access",
                "size": "large",
                "weight": "bolder",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "account_id",
                "label": "Select Account",
                "style": "filtered",
                "choices": account_choices,
                "isRequired": True,
                "errorMessage": "Please select an account",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "permission_set",
                "label": "Select Permission Set",
                "style": "filtered",
                "choices": permission_set_choices,
                "isRequired": True,
                "errorMessage": "Please select a permission set",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "duration",
                "label": "Duration",
                "choices": duration_choices,
                "isRequired": True,
                "errorMessage": "Please select a duration",
            },
            {
                "type": "Input.Text",
                "id": "reason",
                "label": "Reason",
                "isMultiline": True,
                "placeholder": "Reason will be saved in audit logs.",
                "isRequired": True,
                "errorMessage": "Please provide a reason",
            },
        ],
        "actions": [{"type": "Action.Submit", "title": "Request"}],
    }


def build_group_access_form(
    groups: list[entities.aws.SSOGroup],
    duration_options: list[str],
) -> dict:
    """Build Adaptive Card for group access request Task Module."""
    group_choices = [{"title": g.name, "value": g.id} for g in groups]
    duration_choices = [{"title": d, "value": d} for d in duration_options]

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {
                "type": "TextBlock",
                "text": "Request AWS Group Access",
                "size": "large",
                "weight": "bolder",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "group_id",
                "label": "Select Group",
                "style": "filtered",
                "choices": group_choices,
                "isRequired": True,
                "errorMessage": "Please select a group",
            },
            {
                "type": "Input.ChoiceSet",
                "id": "duration",
                "label": "Duration",
                "choices": duration_choices,
                "isRequired": True,
                "errorMessage": "Please select a duration",
            },
            {
                "type": "Input.Text",
                "id": "reason",
                "label": "Reason",
                "isMultiline": True,
                "placeholder": "Reason will be saved in audit logs.",
                "isRequired": True,
                "errorMessage": "Please provide a reason",
            },
        ],
        "actions": [{"type": "Action.Submit", "title": "Request"}],
    }


def build_approval_card(  # noqa: PLR0913
    requester_name: str,
    account: entities.aws.Account | None,
    group: entities.aws.SSOGroup | None,
    role_name: str | None,
    reason: str,
    permission_duration: str,
    show_buttons: bool,
    color_style: str,
    request_data: dict,
    elevator_request_id: str | None = None,
    header_subtitle: str | None = None,
    management_account_id: str | None = None,
) -> dict:
    """Build Adaptive Card for approval request message in channel."""
    if account is not None:
        title = "AWS Account Access Request"
        facts = [
            {"title": "Requester", "value": requester_name},
            {"title": "Account name", "value": account.name},
            {"title": "Account ID", "value": account.id},
            {"title": "Role name", "value": role_name or ""},
            {"title": "Reason", "value": reason},
            {"title": "Duration", "value": permission_duration},
        ]
    else:
        title = "AWS Group Access Request"
        facts = [
            {"title": "Requester", "value": requester_name},
            {"title": "Group", "value": group.name if group else ""},
            {"title": "Reason", "value": reason},
            {"title": "Duration", "value": permission_duration},
        ]

    header_items: list[dict] = [
        {
            "type": "TextBlock",
            "text": title,
            "size": "large",
            "weight": "bolder",
        }
    ]
    if (header_subtitle or "").strip():
        header_items.append(
            {
                "type": "TextBlock",
                "text": (header_subtitle or "").strip(),
                "wrap": True,
                "isSubtle": True,
            }
        )

    body = [
        {
            "type": "Container",
            "style": color_style,
            "items": header_items,
        },
        {
            "type": "FactSet",
            "facts": facts,
        },
    ]
    if account is not None and organizations.is_management_account(account.id, management_account_id):
        body.append(
            {
                "type": "Container",
                "style": "attention",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "⚠️ Warning: this request is for the AWS management account. ⚠️",
                        "wrap": True,
                        "weight": "bolder",
                    }
                ],
            }
        )

    card: dict = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
    }

    if show_buttons:
        button_data = dict(request_data)
        if elevator_request_id:
            button_data["elevator_request_id"] = elevator_request_id

        card["actions"] = [
            {
                "type": "Action.Submit",
                "title": "Approve",
                "style": "positive",
                "data": {**button_data, "action": "approve"},
            },
            {
                "type": "Action.Submit",
                "title": "Discard",
                "style": "destructive",
                "data": {**button_data, "action": "discard"},
            },
        ]

    return card


def update_card_after_decision(
    original_card: dict,
    decision_action: str,
    color_style: str,
    decision_by: str | None = None,
    decision_by_user_id: str | None = None,
) -> dict:
    """Remove ActionSet, add status footer (optionally with actor), update color style."""
    card = copy.deepcopy(original_card)

    # Remove top-level actions (ActionSet)
    card.pop("actions", None)

    # Update header container style
    for item in card.get("body", []):
        if item.get("type") == "Container":
            item["style"] = color_style
            break

    # Append status. When we know the actor's Teams id, embed an Adaptive Card mention so Teams renders
    # it as a clickable @mention. When we don't, fall back to plain text.
    acted_by = (decision_by or "").strip()
    acted_by_id = (decision_by_user_id or "").strip()
    mention_text = f"<at>{acted_by}</at>" if acted_by else ""
    use_mention = bool(acted_by and acted_by_id)
    suffix = f" by {mention_text if use_mention else acted_by}" if acted_by else ""
    if use_mention:
        card["msteams"] = {
            "entities": [
                {
                    "type": "mention",
                    "text": mention_text,
                    "mentioned": {"id": acted_by_id, "name": acted_by},
                }
            ]
        }
    card.setdefault("body", []).append(
        {
            "type": "TextBlock",
            "text": f"Request {decision_action}{suffix}",
            "wrap": True,
            "weight": "bolder",
        }
    )

    return card


def update_card_on_expiry(
    original_card: dict,
    expiration_hours: int,
    expired_style: str,
) -> dict:
    """Remove ActionSet, add expiry footer, set expired style."""
    card = copy.deepcopy(original_card)

    # Remove top-level actions (ActionSet)
    card.pop("actions", None)

    # Update header container style
    for item in card.get("body", []):
        if item.get("type") == "Container":
            item["style"] = expired_style
            break

    # Append expiry footer
    card.setdefault("body", []).append(
        {
            "type": "TextBlock",
            "text": f"Request expired after {expiration_hours} hour(s).",
            "wrap": True,
            "weight": "bolder",
        }
    )

    return card
