import datetime

import pytest

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
        "requester": entities.slack.User(
            email="email@email", id="123", real_name="123"
        ),
        "reason": "",
    }


@pytest.fixture(
    params=[
        {
            "description": """If we have two statements, and one of them explicitly denies (with self_approval = False)
            while the other allows (with self_approval = True), then we should deny self_approval.""",
            "in": {
                "statements": frozenset(
                    [
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": None,
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": False,
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approvers": ["CTO@test.com"],
                                "allow_self_approval": None,
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approval_is_not_required": None,
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approval_is_not_required": False,
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["*"],
                                "permission_set": ["*"],
                                "approval_is_not_required": None,
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                            Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                            Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver@example.com"],
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver1@example.com"],
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
                            {
                                "resource_type": "Account",
                                "resource": ["111111111111"],
                                "permission_set": ["AdministratorAccess"],
                                "approvers": ["approver1@example.com"],
                            }
                        ),
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
                        Statement.parse_obj(
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
        make_decision_on_approve_request(
            **test_cases_for_approve_request_decision["in"]
        )
        == test_cases_for_approve_request_decision["out"]
    )


def test_execute_access_request_decision(
    test_cases_for_access_request_decision,
    execute_decision_info,
):
    if test_cases_for_access_request_decision["out"].grant is not True:
        assert (
            execute_decision(
                decision=test_cases_for_access_request_decision["out"],
                **execute_decision_info
            )
            is False
        )


def test_execute_approve_request_decision(
    test_cases_for_approve_request_decision,
    execute_decision_info,
):
    if test_cases_for_approve_request_decision["out"].grant is not True:
        assert (
            execute_decision(
                decision=test_cases_for_approve_request_decision["out"],
                **execute_decision_info
            )
            is False
        )


def test_make_and_excute_access_request_decision(
    test_cases_for_access_request_decision,
    execute_decision_info,
):
    decision = make_decision_on_access_request(
        **test_cases_for_access_request_decision["in"]
    )
    if decision.grant is not True:
        assert execute_decision(decision=decision, **execute_decision_info) is False


def test_make_and_excute_approve_request_decision(
    test_cases_for_approve_request_decision,
    execute_decision_info,
):
    decision = make_decision_on_approve_request(
        **test_cases_for_approve_request_decision["in"]
    )
    if decision.grant is not True:
        assert execute_decision(decision=decision, **execute_decision_info) is False
