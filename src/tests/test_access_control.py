import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import access_control
import entities
from access_control import (
    AccessRequestDecision,
    ApproveRequestDecision,
    DecisionReason,
    execute_decision,
    make_decision_on_access_request,
    make_decision_on_approve_request,
)
from statement import Statement

# ruff: noqa: ANN201, ANN001


@pytest.fixture
def execute_decision_info():
    return {
        "permission_set_name": "1233321",
        "account_id": "1233321",
        "permission_duration": datetime.timedelta(days=1),
        "approver": entities.slack.User(email="email@email", id="123", real_name="123"),
        "requester": entities.slack.User(email="email@email", id="123", real_name="123"),
        "reason": "",
        # execute_decision no longer defaults these itself (#194 duplication
        # cleanup): both production call sites always pass them explicitly,
        # so the only place still relying on a default was this fixture --
        # baseline "slack"/"NA"/"NA" values here instead, matching what the
        # removed defaults used to provide. Tests exercising the "cli" path
        # override these via {**execute_decision_info, ...} rather than a
        # duplicate keyword argument (passing the same key both via **dict
        # and explicitly is a TypeError).
        "request_source": "slack",
        "verified_arn": "NA",
        "verified_user_id": "NA",
    }


@pytest.fixture(
    params=[
        {
            "description": """If we have two statements, and one of them explicitly denies (with self_approval = False)
            while the other allows (with self_approval = True), then we should deny self_approval.""",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": [
                                    "CTO@test.com",
                                ],
                                "allow_self_approval": True,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": [
                                    "Approver2@test.com",
                                    "CTO@test.com",
                                ],
                                "allow_self_approval": False,
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "CTO@test.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.RequiresApproval,
                approvers=frozenset(["Approver2@test.com"]),
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": [
                                    "CTO@test.com",
                                ],
                                "allow_self_approval": True,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["Approver2@test.com", "CTO@test.com"],
                                "allow_self_approval": False,
                            }
                        ),
                    ]
                ),
            ),
        },
        {
            "description": "Test where allow_self_approval is set to None",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": None,
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "CTO@test.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoApprovers,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": None,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "Test where allow_self_approval has mixed values of None and False",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": None,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": False,
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "CTO@test.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoApprovers,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": False,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": None,
                            }
                        ),
                    ]
                ),
            ),
        },
        {
            "description": "Test where allow_self_approval has mixed values of None and True",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": None,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": True,
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "CTO@test.com",
            },
            "out": AccessRequestDecision(
                grant=True,
                reason=DecisionReason.SelfApproval,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": True,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "Test where approval_is_not_required is set to None",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": [
                                    "Approver2@test.com",
                                    "CTO@test.com",
                                ],
                                "approval_is_not_required": None,
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "Approver2@test.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.RequiresApproval,
                approvers=frozenset(["CTO@test.com"]),
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": [
                                    "Approver2@test.com",
                                    "CTO@test.com",
                                ],
                                "approval_is_not_required": None,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "Test where approval_is_not_required has mixed values of None and False",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approval_is_not_required": None,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approval_is_not_required": False,
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "anybody@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoApprovers,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approval_is_not_required": False,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approval_is_not_required": None,
                            }
                        ),
                    ]
                ),
            ),
        },
        {
            "description": "Test where approval_is_not_required has mixed values of None and True",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approval_is_not_required": None,
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approval_is_not_required": True,
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "anybody@example.com",
            },
            "out": AccessRequestDecision(
                grant=True,
                reason=DecisionReason.ApprovalNotRequired,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approval_is_not_required": True,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "test allow_self_approval and approval_is_not_required None values",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["anybody@example.com"],
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "anybody@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoApprovers,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["anybody@example.com"],
                                "approval_is_not_required": None,
                                "allow_self_approval": None,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "Grant access if approval is not required",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approval_is_not_required": True,
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "anybody@example.com",
            },
            "out": AccessRequestDecision(
                grant=True,
                reason=DecisionReason.ApprovalNotRequired,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approval_is_not_required": True,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "Request requires approval if requester is not an approver",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["one@example.com"],
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "second@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.RequiresApproval,
                approvers=frozenset(["one@example.com"]),
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["one@example.com"],
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "requester is not an approver and self approval is allowed - RequiresApproval",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["one@example.com"],
                                "allow_self_approval": True,
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "second@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.RequiresApproval,
                approvers=frozenset(["one@example.com"]),
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["one@example.com"],
                                "allow_self_approval": True,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "requester is an approver, but self approval is not allowed, and there is other approver - RequiresApproval",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": [
                                    "approver@example.com",
                                    "approver2@example.com",
                                ],
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "approver@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.RequiresApproval,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": [
                                    "approver@example.com",
                                    "approver2@example.com",
                                ],
                            }
                        )
                    ]
                ),
                approvers=frozenset({"approver2@example.com"}),
            ),
        },
        {
            "description": "self approval is allowed and requester is approver -SelfApproval",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["approver@example.com"],
                                "allow_self_approval": True,
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "approver@example.com",
            },
            "out": AccessRequestDecision(
                grant=True,
                reason=DecisionReason.SelfApproval,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["approver@example.com"],
                                "allow_self_approval": True,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "no approvers - NoApprovers",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "example@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoApprovers,
                based_on_statements=frozenset(
                    frozenset(
                        [
                            Statement.model_validate(
                                {
                                    "resource_type": "Account",
                                    "resource": ["*"],
                                    "permission_set": ["*"],
                                }
                            )
                        ]
                    )
                ),
            ),
        },
        {
            "description": "requester is an approver, but self approval is not allowed - NoApprovers",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "approver@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoApprovers,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "no approvers but self approval is allowed - NoApprovers",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "allow_self_approval": True,
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "example@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoApprovers,
                based_on_statements=frozenset(
                    frozenset(
                        [
                            Statement.model_validate(
                                {
                                    "resource_type": "Account",
                                    "resource": ["*"],
                                    "permission_set": ["*"],
                                    "allow_self_approval": True,
                                }
                            )
                        ]
                    )
                ),
            ),
        },
        {
            "description": "statement is not affected by the access request - NoStatements",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["ReadOnlyAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "requester@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.NoStatements,
                based_on_statements=frozenset([]),
            ),
        },
        {
            "description": "multiple statements affecting the access request, some require approval and some don't",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approval_is_not_required": True,
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "approver@example.com",
            },
            "out": AccessRequestDecision(
                grant=True,
                reason=DecisionReason.ApprovalNotRequired,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approval_is_not_required": True,
                            }
                        ),
                    ]
                ),
            ),
        },
        {
            "description": "multiple statements affecting the access request, with different sets of approvers.",
            "in": {
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver1@example.com"],
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver2@example.com"],
                            }
                        ),
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "requester@example.com",
            },
            "out": AccessRequestDecision(
                grant=False,
                reason=DecisionReason.RequiresApproval,
                approvers=frozenset(["approver1@example.com", "approver2@example.com"]),
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver1@example.com"],
                            }
                        ),
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver2@example.com"],
                            }
                        ),
                    ]
                ),
            ),
        },
    ],
    ids=lambda t: t["description"],
)
def test_cases_for_access_request_decision(request):
    return request.param


@pytest.fixture(
    params=[
        {
            "description": "approver is approver",
            "in": {
                "action": entities.ApproverAction.Approve,
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "requester@example.com",
                "approver_email": "approver@example.com",
            },
            "out": ApproveRequestDecision(
                grant=True,
                permit=True,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "approver is approver but self approval is not allowed",
            "in": {
                "action": entities.ApproverAction.Approve,
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                                "allow_self_approval": False,
                            }
                        )
                    ]
                ),
                "account_id": "111111111111",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "approver@example.com",
                "approver_email": "approver@example.com",
            },
            "out": ApproveRequestDecision(
                grant=False,
                permit=False,
                based_on_statements=frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                                "allow_self_approval": False,
                            }
                        )
                    ]
                ),
            ),
        },
        {
            "description": "approver is not an approver",
            "in": {
                "action": entities.ApproverAction.Approve,
                "statements": frozenset(
                    [
                        Statement.model_validate(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        )
                    ]
                ),
                "account_id": "222222222222",
                "permission_set_name": "AdministratorAccess",
                "requester_email": "requester@example.com",
                "approver_email": "notapprover@example.com",
            },
            "out": ApproveRequestDecision(
                grant=False,
                permit=False,
                based_on_statements=frozenset(),
            ),
        },
    ],
    ids=lambda t: t["description"],
)
def test_cases_for_approve_request_decision(request):
    return request.param


def test_make_decision_on_access_request(test_cases_for_access_request_decision):
    actual = make_decision_on_access_request(**test_cases_for_access_request_decision["in"])
    expected = test_cases_for_access_request_decision["out"]

    # Compare grant and reason attributes directly
    assert actual.grant == expected.grant
    assert actual.reason == expected.reason

    # Compare based_on_statements attributes as sets to ignore order
    assert set(actual.based_on_statements) == set(expected.based_on_statements)

    # Compare approvers attributes directly (assuming it is not a set/frozenset)
    assert actual.approvers == expected.approvers


def test_make_decision_on_approve_request(test_cases_for_approve_request_decision):
    assert (
        make_decision_on_approve_request(**test_cases_for_approve_request_decision["in"]) == test_cases_for_approve_request_decision["out"]
    )


def test_make_decision_on_approve_request_recognizes_self_approval_despite_case_mismatch():
    """Regression test for a real self-approval bypass found in a final
    pre-delivery review: for a CLI-sourced request, requester_email passed
    in here is the pinned, verified Identity Center email
    (handle_button_click's eligibility_email), while approver_email is
    whoever clicked Approve's *current Slack profile* email -- two
    different identity sources for the same physical person. A raw `==`
    comparison made a same-person click that merely differed in case
    register as is_self_approval=False, which the decision's own boolean
    (`is_self_approval and allow_self_approval or not is_self_approval`)
    then treated as "a different, legitimate approver approved this" --
    silently permitting the grant even though allow_self_approval is
    explicitly false for this statement. The same person must be
    recognized as such regardless of case."""
    # Domain kept lowercase throughout, deliberately: pydantic's EmailStr
    # lowercases the *domain* part on its own (verified separately -- e.g.
    # "Alice@Corp.com" becomes "Alice@corp.com"), which would otherwise
    # make approver_email fail the `in statement.approvers` membership
    # check for an unrelated reason and never even reach the
    # is_self_approval comparison this test means to isolate. Only the
    # *local* part's case differs between approver_email and
    # requester_email here -- EmailStr leaves that alone, so this cleanly
    # exercises just the fix.
    statement = Statement.model_validate(
        {
            "resource_type": "Account",
            "resource": ["111111111111"],
            "permission_set": ["AdministratorAccess"],
            "approvers": ["Alice@corp.com"],
            "allow_self_approval": False,
        }
    )
    decision = make_decision_on_approve_request(
        action=entities.ApproverAction.Approve,
        statements=frozenset([statement]),
        account_id="111111111111",
        permission_set_name="AdministratorAccess",
        # Exactly matches the statement's own approvers entry, so the
        # membership check passes regardless of normalization -- isolating
        # is_self_approval as the only thing left that can differ.
        approver_email="Alice@corp.com",
        # The requester's pinned, verified email differs from the approver
        # entry only by local-part case -- the same person, resolved via a
        # different identity source (exactly what a CLI request's
        # verified_email vs. a Slack profile email produces).
        requester_email="alice@corp.com",
    )
    assert decision.permit is False, "a case-differing self-approval must still be recognized as self-approval and denied"
    assert decision.grant is False


def test_make_decision_on_approve_request_still_allows_self_approval_when_permitted():
    """Companion to the test above: when allow_self_approval is true, a
    case-differing self-approval must still be *granted*, not accidentally
    denied by the same normalization fix -- this proves the fix correctly
    recognizes the match (not just happens to fail closed)."""
    statement = Statement.model_validate(
        {
            "resource_type": "Account",
            "resource": ["111111111111"],
            "permission_set": ["AdministratorAccess"],
            "approvers": ["Alice@corp.com"],
            "allow_self_approval": True,
        }
    )
    decision = make_decision_on_approve_request(
        action=entities.ApproverAction.Approve,
        statements=frozenset([statement]),
        account_id="111111111111",
        permission_set_name="AdministratorAccess",
        approver_email="Alice@corp.com",
        requester_email="alice@corp.com",
    )
    assert decision.permit is True
    assert decision.grant is True


def test_make_decision_on_approve_request_still_denies_a_different_person_approving():
    """Companion test: normalizing case must not make two genuinely
    different people's emails collide -- only an exact case-insensitive
    match should be treated as the same person."""
    statement = Statement.model_validate(
        {
            "resource_type": "Account",
            "resource": ["111111111111"],
            "permission_set": ["AdministratorAccess"],
            "approvers": ["alice@corp.com", "bob@corp.com"],
            "allow_self_approval": False,
        }
    )
    decision = make_decision_on_approve_request(
        action=entities.ApproverAction.Approve,
        statements=frozenset([statement]),
        account_id="111111111111",
        permission_set_name="AdministratorAccess",
        approver_email="Bob@corp.com",
        requester_email="alice@corp.com",
    )
    # Bob approving Alice's request is a normal, legitimate approval, not a
    # self-approval -- must still be permitted.
    assert decision.permit is True
    assert decision.grant is True


def test_execute_access_request_decision(
    test_cases_for_access_request_decision,
    execute_decision_info,
):
    if test_cases_for_access_request_decision["out"].grant is not True:
        # #98: a terminal denial (NoStatements/NoApprovers/RequesterNotAllowed)
        # now logs a "declined" audit entry before returning -- mocked here
        # so these otherwise-unrelated cases don't hit the real S3 client.
        with patch.object(access_control.s3, "log_operation"):
            assert execute_decision(decision=test_cases_for_access_request_decision["out"], **execute_decision_info) is False


def test_execute_approve_request_decision(
    test_cases_for_approve_request_decision,
    execute_decision_info,
):
    if test_cases_for_approve_request_decision["out"].grant is not True:
        assert execute_decision(decision=test_cases_for_approve_request_decision["out"], **execute_decision_info) is False


def test_make_and_excute_access_request_decision(
    test_cases_for_access_request_decision,
    execute_decision_info,
):
    decision = make_decision_on_access_request(**test_cases_for_access_request_decision["in"])
    if decision.grant is not True:
        # #98: see test_execute_access_request_decision above for why this is mocked.
        with patch.object(access_control.s3, "log_operation"):
            assert execute_decision(decision=decision, **execute_decision_info) is False


def test_make_and_excute_approve_request_decision(
    test_cases_for_approve_request_decision,
    execute_decision_info,
):
    decision = make_decision_on_approve_request(**test_cases_for_approve_request_decision["in"])
    if decision.grant is not True:
        assert execute_decision(decision=decision, **execute_decision_info) is False


def test_execute_decision_grants_against_verified_user_id_for_cli_requests_without_reresolving_email(execute_decision_info):
    """Regression test for the UserId-threading gap: a CLI request's
    identity is verified once, at submission time, against a specific
    UserId (cli_auth.extract_identity, cross-checked again by
    handle_cli_access_request's email round-trip) -- execute_decision must
    grant against that exact UserId, not re-resolve requester.email through
    a second, independent Identity Store lookup at approval time that could
    disagree with the one actually verified (a directory change in between,
    or a primary-email lookup that legitimately falls through to a
    different person via the secondary-domain fallback). Verified by
    asserting get_user_principal_id_by_email is never even called when a
    CLI verified_user_id is supplied, and that the account assignment and
    audit entry are both created against that exact UserId."""
    decision = AccessRequestDecision(grant=True, reason=DecisionReason.SelfApproval, based_on_statements=frozenset())
    verified_user_id = "u-verified-from-cli-session"

    with (
        patch.object(
            access_control.sso,
            "describe_sso_instance",
            return_value=SimpleNamespace(arn="arn:aws:sso:::instance/ssoins-1", identity_store_id="d-1234"),
        ),
        patch.object(
            access_control.sso,
            "get_permission_set_by_name",
            return_value=SimpleNamespace(
                arn="arn:aws:sso:::permissionSet/ssoins-1/ps-1", name=execute_decision_info["permission_set_name"]
            ),
        ),
        patch.object(access_control.sso, "get_user_principal_id_by_email") as mock_resolve_by_email,
        patch.object(
            access_control.sso, "create_account_assignment_and_wait_for_result", return_value=SimpleNamespace(request_id="req-1")
        ) as mock_create_assignment,
        patch.object(access_control.schedule, "schedule_revoke_event"),
        patch.object(access_control.s3, "log_operation") as mock_log_operation,
    ):
        result = execute_decision(
            decision=decision,
            **{
                **execute_decision_info,
                "request_source": "cli",
                "verified_arn": "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req@example.com",
                "verified_user_id": verified_user_id,
            },
        )

    assert result is True
    mock_resolve_by_email.assert_not_called()
    account_assignment = mock_create_assignment.call_args.args[1]
    assert account_assignment.user_principal_id == verified_user_id
    audit_entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert audit_entry.sso_user_principal_id == verified_user_id
    assert audit_entry.secondary_domain_was_used is False


def test_execute_decision_still_resolves_by_email_for_slack_requests(execute_decision_info):
    """Companion to the test above: a "slack" request has no submission-time
    -verified UserId to grant against -- execute_decision must fall back to
    the pre-existing email-based resolution unchanged, not silently skip the
    grant or use a placeholder UserId. See
    test_execute_decision_fails_closed_for_a_cli_request_missing_verified_user_id
    for why a "cli" request with no verified_user_id is handled differently
    (#194 B6): unlike a "slack" request, it must not silently fall back to
    this same email-based resolution."""
    decision = AccessRequestDecision(grant=True, reason=DecisionReason.SelfApproval, based_on_statements=frozenset())
    resolved_user_id = "u-resolved-by-email"

    with (
        patch.object(
            access_control.sso,
            "describe_sso_instance",
            return_value=SimpleNamespace(arn="arn:aws:sso:::instance/ssoins-1", identity_store_id="d-1234"),
        ),
        patch.object(
            access_control.sso,
            "get_permission_set_by_name",
            return_value=SimpleNamespace(
                arn="arn:aws:sso:::permissionSet/ssoins-1/ps-1", name=execute_decision_info["permission_set_name"]
            ),
        ),
        patch.object(access_control.sso, "get_user_principal_id_by_email", return_value=(resolved_user_id, False)) as mock_resolve_by_email,
        patch.object(
            access_control.sso, "create_account_assignment_and_wait_for_result", return_value=SimpleNamespace(request_id="req-1")
        ) as mock_create_assignment,
        patch.object(access_control.schedule, "schedule_revoke_event"),
        patch.object(access_control.s3, "log_operation"),
    ):
        result = execute_decision(decision=decision, **execute_decision_info)

    assert result is True
    mock_resolve_by_email.assert_called_once()
    account_assignment = mock_create_assignment.call_args.args[1]
    assert account_assignment.user_principal_id == resolved_user_id


def test_execute_decision_fails_closed_for_a_cli_request_missing_verified_user_id(execute_decision_info):
    """Regression test (#194 B6): a request_source="cli" request with
    verified_user_id still "NA" -- the only way this combination occurs is a
    pending approval message posted before verified_user_id existed on this
    field -- must not silently fall back to the email-based resolution
    "slack" requests use. That fallback includes the secondary-domain
    fuzzy-match mechanism, which is exactly what the CLI's stricter,
    submission-time-verified identity path exists to avoid trusting; a
    message explicitly labeled "Source: CLI" silently using it anyway would
    defeat the whole point. Verified by asserting the grant is refused
    (raises) before either get_user_principal_id_by_email or
    create_account_assignment_and_wait_for_result is ever called -- nothing
    is granted through the weaker mechanism, not even accidentally."""
    decision = AccessRequestDecision(grant=True, reason=DecisionReason.SelfApproval, based_on_statements=frozenset())

    with (
        patch.object(
            access_control.sso,
            "describe_sso_instance",
            return_value=SimpleNamespace(arn="arn:aws:sso:::instance/ssoins-1", identity_store_id="d-1234"),
        ),
        patch.object(
            access_control.sso,
            "get_permission_set_by_name",
            return_value=SimpleNamespace(
                arn="arn:aws:sso:::permissionSet/ssoins-1/ps-1", name=execute_decision_info["permission_set_name"]
            ),
        ),
        patch.object(access_control.sso, "get_user_principal_id_by_email") as mock_resolve_by_email,
        patch.object(access_control.sso, "create_account_assignment_and_wait_for_result") as mock_create_assignment,
        patch.object(access_control.s3, "log_operation") as mock_log_operation,
        pytest.raises(ValueError, match="no verified UserId"),
    ):
        execute_decision(
            decision=decision,
            **{
                **execute_decision_info,
                "request_source": "cli",
                "verified_arn": "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_Admin/req@example.com",
                "verified_user_id": "NA",
            },
        )

    mock_resolve_by_email.assert_not_called()
    mock_create_assignment.assert_not_called()

    # #98: the fail-closed refusal above is itself an incomplete grant
    # attempt -- it must not vanish with no audit trail just because it
    # failed before reaching AWS.
    audit_entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert audit_entry.operation_type == "incomplete"
    assert audit_entry.sso_user_principal_id == "NA"
    assert "no verified UserId" in audit_entry.error_message


def test_get_requester_group_ids_uses_verified_user_id_directly_for_cli_requests():
    """Regression test (#194 B4 residual, found live by Andrey Devyatkin):
    when a CLI request's already-verified UserId is available, group
    membership must be looked up directly against that principal via
    sso.list_groups_for_user -- not re-derived by resolving requester_email
    through get_user_principal_id_by_email, which applies the same
    secondary-domain fuzzy-match fallback the CLI identity path elsewhere
    explicitly distrusts. Re-deriving could resolve to a *different*
    principal than verified_user_id, the one actually being granted access
    -- making the eligibility decision and the grant target evaluate two
    different people. Verified by asserting get_user_principal_id_by_email
    is never called at all, not just that the right groups come back."""
    expected_group_ids = frozenset({"g-1", "g-2"})

    with (
        patch.object(
            access_control.sso,
            "describe_sso_instance",
            return_value=SimpleNamespace(arn="arn:aws:sso:::instance/ssoins-1", identity_store_id="d-1234"),
        ),
        patch.object(access_control.sso, "get_user_principal_id_by_email") as mock_resolve_by_email,
        patch.object(access_control.sso, "list_groups_for_user", return_value=expected_group_ids) as mock_list_groups,
    ):
        result = access_control.get_requester_group_ids("req@example.com", verified_user_id="u-verified-from-cli-session")

    assert result == expected_group_ids
    mock_resolve_by_email.assert_not_called()
    mock_list_groups.assert_called_once_with("d-1234", "u-verified-from-cli-session", access_control.identitystore_client)


def test_get_requester_group_ids_still_resolves_by_email_when_no_verified_user_id():
    """Companion to the test above: without a verified_user_id (the Slack
    modal path, which has no pre-verified identity of its own), group
    membership resolution must fall back to the pre-existing
    email-based lookup unchanged."""
    expected_group_ids = frozenset({"g-3"})
    resolved_user_id = "u-resolved-by-email"

    with (
        patch.object(
            access_control.sso,
            "describe_sso_instance",
            return_value=SimpleNamespace(arn="arn:aws:sso:::instance/ssoins-1", identity_store_id="d-1234"),
        ),
        patch.object(access_control.sso, "get_user_principal_id_by_email", return_value=(resolved_user_id, False)) as mock_resolve_by_email,
        patch.object(access_control.sso, "list_groups_for_user", return_value=expected_group_ids) as mock_list_groups,
    ):
        result = access_control.get_requester_group_ids("req@example.com")

    assert result == expected_group_ids
    mock_resolve_by_email.assert_called_once()
    mock_list_groups.assert_called_once_with("d-1234", resolved_user_id, access_control.identitystore_client)


# ---------------------------------------------------------------------------
# #98: audit entries for declined and incomplete requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "denial_reason",
    [DecisionReason.NoStatements, DecisionReason.NoApprovers, DecisionReason.RequesterNotAllowed],
)
def test_execute_decision_logs_declined_entry_for_terminal_denial_reasons(denial_reason, execute_decision_info):
    """A request that's auto-denied outright (as opposed to RequiresApproval,
    still pending a human) must leave an audit trail explaining why, not just
    a Slack message -- that's the actual gap issue #98 is about."""
    decision = AccessRequestDecision(grant=False, reason=denial_reason, based_on_statements=frozenset())

    with patch.object(access_control.s3, "log_operation") as mock_log_operation:
        result = execute_decision(decision=decision, **execute_decision_info)

    assert result is False
    audit_entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert audit_entry.operation_type == "declined"
    assert audit_entry.decision_reason == denial_reason.value
    assert audit_entry.sso_user_principal_id == "NA"
    assert audit_entry.audit_entry_type == "account"


def test_execute_decision_does_not_log_an_entry_for_requires_approval():
    """RequiresApproval means the request is still pending a human decision,
    not declined -- logging it here would be premature (and, since the
    eventual Approve/Discard click logs its own entry, a duplicate)."""
    decision = AccessRequestDecision(grant=False, reason=DecisionReason.RequiresApproval, based_on_statements=frozenset())

    with patch.object(access_control.s3, "log_operation") as mock_log_operation:
        result = execute_decision(
            decision=decision,
            permission_set_name="Foo",
            account_id="111111111111",
            permission_duration=datetime.timedelta(days=1),
            approver=entities.slack.User(email="email@email", id="123", real_name="123"),
            requester=entities.slack.User(email="email@email", id="123", real_name="123"),
            reason="",
            request_source="slack",
            verified_arn="NA",
            verified_user_id="NA",
        )

    assert result is False
    mock_log_operation.assert_not_called()


def test_execute_decision_logs_incomplete_entry_when_the_account_assignment_fails(execute_decision_info):
    """A request that was approved but never actually granted -- the AWS
    call itself failed -- must not vanish with no audit trail just because
    it never reached the existing "grant" log_operation call below it."""
    decision = AccessRequestDecision(grant=True, reason=DecisionReason.SelfApproval, based_on_statements=frozenset())

    with (
        patch.object(
            access_control.sso,
            "describe_sso_instance",
            return_value=SimpleNamespace(arn="arn:aws:sso:::instance/ssoins-1", identity_store_id="d-1234"),
        ),
        patch.object(
            access_control.sso,
            "get_permission_set_by_name",
            return_value=SimpleNamespace(
                arn="arn:aws:sso:::permissionSet/ssoins-1/ps-1", name=execute_decision_info["permission_set_name"]
            ),
        ),
        patch.object(access_control.sso, "get_user_principal_id_by_email", return_value=("u-resolved", False)),
        patch.object(
            access_control.sso,
            "create_account_assignment_and_wait_for_result",
            side_effect=RuntimeError("boom: IAM Identity Center throttled"),
        ),
        patch.object(access_control.s3, "log_operation") as mock_log_operation,
        pytest.raises(RuntimeError, match="boom"),
    ):
        execute_decision(decision=decision, **execute_decision_info)

    audit_entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert audit_entry.operation_type == "incomplete"
    # Identity resolution succeeded before the actual AWS call failed, so the
    # entry still reports who it would have been granted to.
    assert audit_entry.sso_user_principal_id == "u-resolved"
    assert "boom" in audit_entry.error_message


def test_execute_decision_on_group_request_logs_declined_entry_for_terminal_denial_reasons():
    """Mirrors test_execute_decision_logs_declined_entry_for_terminal_denial_reasons
    for the group-request path, which funnels through its own copy of the
    same terminal-reasons check."""
    decision = AccessRequestDecision(grant=False, reason=DecisionReason.NoApprovers, based_on_statements=frozenset())
    group = entities.aws.SSOGroup(name="TestGroup", id="g-1234", description="test", identity_store_id="d-1234")

    with patch.object(access_control.s3, "log_operation") as mock_log_operation:
        result = access_control.execute_decision_on_group_request(
            decision=decision,
            group=group,
            permission_duration=datetime.timedelta(days=1),
            approver=entities.slack.User(email="email@email", id="123", real_name="123"),
            requester=entities.slack.User(email="email@email", id="123", real_name="123"),
            reason="",
            identity_store_id="d-1234",
        )

    assert result is False
    audit_entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert audit_entry.operation_type == "declined"
    assert audit_entry.decision_reason == "NoApprovers"
    assert audit_entry.audit_entry_type == "group"
    assert audit_entry.group_id == "g-1234"


def test_execute_decision_on_group_request_logs_incomplete_entry_when_group_membership_fails():
    """Mirrors test_execute_decision_logs_incomplete_entry_when_the_account_assignment_fails
    for the group-request path: an approved group request whose actual AWS
    call (adding the user to the group) fails must still leave an audit
    trail, not vanish silently."""
    decision = AccessRequestDecision(grant=True, reason=DecisionReason.SelfApproval, based_on_statements=frozenset())
    group = entities.aws.SSOGroup(name="TestGroup", id="g-1234", description="test", identity_store_id="d-1234")

    with (
        patch.object(access_control.sso, "get_user_principal_id_by_email", return_value=("u-resolved", False)),
        patch.object(
            access_control.sso,
            "describe_sso_instance",
            return_value=SimpleNamespace(arn="arn:aws:sso:::instance/ssoins-1", identity_store_id="d-1234"),
        ),
        patch.object(access_control.sso, "is_user_in_group", return_value=None),
        patch.object(access_control.sso, "add_user_to_a_group", side_effect=RuntimeError("boom: group not found")),
        patch.object(access_control.s3, "log_operation") as mock_log_operation,
        pytest.raises(RuntimeError, match="boom"),
    ):
        access_control.execute_decision_on_group_request(
            decision=decision,
            group=group,
            permission_duration=datetime.timedelta(days=1),
            approver=entities.slack.User(email="email@email", id="123", real_name="123"),
            requester=entities.slack.User(email="email@email", id="123", real_name="123"),
            reason="",
            identity_store_id="d-1234",
        )

    audit_entry = mock_log_operation.call_args.kwargs["audit_entry"]
    assert audit_entry.operation_type == "incomplete"
    assert audit_entry.sso_user_principal_id == "u-resolved"
    assert "boom" in audit_entry.error_message
