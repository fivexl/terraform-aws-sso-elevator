from hypothesis import given, settings

import permissions

from . import strategies

@given(
    permission_set_name=strategies.aws_permission_set_name,
    account_id=strategies.aws_account_id,
)
@settings(max_examples=10)
def test_statement_affects(permission_set_name: str, account_id: str):
    statement = permissions.Statement(
        resource_type="Account",
        resource=frozenset([account_id]),
        permission_set=frozenset([permission_set_name]),
        approvers=None,
        approval_is_not_required=True,
        allow_self_approval=False,
    )
    assert statement.affects(
        account_id=account_id,
        permission_set_name=permission_set_name,
    )


@given(
    permission_set_name=strategies.aws_permission_set_name,
    account_id=strategies.aws_account_id,
)
@settings(max_examples=10)
def test_get_affected_statements(permission_set_name: str, account_id: str):
    statement = permissions.Statement(
        resource_type="Account",
        resource=frozenset([account_id]),
        permission_set=frozenset([permission_set_name]),
        approvers=None,
    )
    assert permissions.get_affected_statements(
        statements=frozenset([statement]),
        account_id=account_id,
        permission_set_name=permission_set_name,
    ) == [statement]


@given(
    permission_set_name=strategies.aws_permission_set_name,
    account_id=strategies.aws_account_id,
)
@settings(max_examples=10)
def test_make_decision_on_request_approval_is_not_required(permission_set_name: str, account_id: str):
    statements = frozenset(
        [
            permissions.Statement(
                resource_type="Account",
                resource=frozenset([account_id]),
                permission_set=frozenset([permission_set_name]),
                approvers=None,
                approval_is_not_required=True,
                allow_self_approval=False,
            )
        ]
    )
    assert isinstance(
        permissions.make_decision_on_request(
            statements=statements,
            account_id=account_id,
            permission_set_name=permission_set_name,
            requester_email="anybody",
        ),
        permissions.ApprovalIsNotRequired,
    )



@given(
    permission_set_name=strategies.aws_permission_set_name,
    account_id=strategies.aws_account_id,
    approvers=strategies.statement_approvers,
)
@settings(max_examples=10)
def test_make_decision_on_request_requires_approval(permission_set_name: str, account_id: str, approvers: frozenset[str]):
    statements = frozenset(
        [
            permissions.Statement(
                resource_type="Account",
                resource=frozenset([account_id]),
                permission_set=frozenset([permission_set_name]),
                approvers=approvers,
            )
        ]
    )
    desision = permissions.make_decision_on_request(
        statements=statements,
        account_id=account_id,
        permission_set_name=permission_set_name,
        requester_email="anybody",
    )
    assert isinstance(desision, permissions.RequiresApproval)
    assert desision.approvers == approvers

