"""Adaptive Card builders for Microsoft Teams integration.

Pure functions that build Adaptive Card JSON structures.
Equivalent of the Block Kit building code in slack_helpers.py.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import entities.aws

_DURATION_PARTS = 3


def parse_duration_choice(duration_str: str) -> timedelta:
    """Parse HH:MM:SS duration from Teams Adaptive Card `duration` input."""
    try:
        parts = duration_str.split(":")
        if len(parts) == _DURATION_PARTS:
            return timedelta(hours=int(parts[0]), minutes=int(parts[1]), seconds=int(parts[2]))
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


def build_account_access_form(
    accounts: list[entities.aws.Account],
    permission_sets: list[entities.aws.PermissionSet],
    duration_options: list[str],
) -> dict:
    """Build Adaptive Card for account access request Task Module."""
    account_choices = [{"title": f"{a.name} ({a.id})", "value": a.id} for a in accounts]
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
) -> dict:
    """Build Adaptive Card for approval request message in channel."""
    if account is not None:
        title = "AWS Account Access Request"
        facts = [
            {"title": "Requester", "value": requester_name},
            {"title": "Account", "value": f"{account.name} ({account.id})"},
            {"title": "Role", "value": role_name or ""},
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

    body = [
        {
            "type": "Container",
            "style": color_style,
            "items": [
                {
                    "type": "TextBlock",
                    "text": title,
                    "size": "large",
                    "weight": "bolder",
                }
            ],
        },
        {
            "type": "FactSet",
            "facts": facts,
        },
    ]

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
    approver_name: str,
    color_style: str,
) -> dict:
    """Remove ActionSet, add decision footer, update color style."""
    card = copy.deepcopy(original_card)

    # Remove top-level actions (ActionSet)
    card.pop("actions", None)

    # Update header container style
    for item in card.get("body", []):
        if item.get("type") == "Container":
            item["style"] = color_style
            break

    # Append decision footer
    card.setdefault("body", []).append(
        {
            "type": "TextBlock",
            "text": f"Request {decision_action} by {approver_name}",
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
