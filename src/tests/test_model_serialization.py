"""Tests for model serialization, particularly handling frozensets with nested Pydantic models."""

from access_control import AccessRequestDecision, ApproveRequestDecision, DecisionReason
from statement import Statement, GroupStatement


class TestModelSerialization:
    """Test that Pydantic models can be serialized to dict without errors."""

    def test_access_request_decision_with_statements_dict_serialization(self):
        """Test that AccessRequestDecision.dict() works with frozenset of Statement objects."""
        statement = Statement.model_validate(
            {
                "resource_type": "Account",
                "resource": ["123456789012"],
                "permission_set": ["AdminAccess"],
                "approvers": ["approver@example.com"],
                "allow_self_approval": True,
            }
        )

        decision = AccessRequestDecision(
            grant=True,
            reason=DecisionReason.SelfApproval,
            based_on_statements=frozenset([statement]),
            approvers=frozenset(["approver@example.com"]),
        )

        # This should not raise TypeError: unhashable type: 'dict'
        result = decision.dict()

        assert isinstance(result, dict)
        assert result["grant"] is True
        assert result["reason"] == DecisionReason.SelfApproval.value
        assert "based_on_statements" in result
        # Frozensets are converted to lists for JSON serialization
        assert isinstance(result["based_on_statements"], list)
        assert len(result["based_on_statements"]) == 1

    def test_approve_request_decision_with_statements_dict_serialization(self):
        """Test that ApproveRequestDecision.dict() works with frozenset of Statement objects."""
        statement = Statement.model_validate(
            {
                "resource_type": "Account",
                "resource": ["123456789012"],
                "permission_set": ["AdminAccess"],
                "approvers": ["approver@example.com"],
                "allow_self_approval": False,
            }
        )

        decision = ApproveRequestDecision(
            grant=True,
            permit=True,
            based_on_statements=frozenset([statement]),
        )

        # This should not raise TypeError: unhashable type: 'dict'
        result = decision.dict()

        assert isinstance(result, dict)
        assert result["grant"] is True
        assert result["permit"] is True
        assert "based_on_statements" in result
        assert isinstance(result["based_on_statements"], list)
        assert len(result["based_on_statements"]) == 1

    def test_access_request_decision_with_group_statements_dict_serialization(self):
        """Test that AccessRequestDecision.dict() works with frozenset of GroupStatement objects."""
        group_statement = GroupStatement.model_validate(
            {
                "resource": ["11111111-2222-3333-4444-555555555555"],
                "approvers": ["approver@example.com"],
                "allow_self_approval": True,
            }
        )

        decision = AccessRequestDecision(
            grant=False,
            reason=DecisionReason.RequiresApproval,
            based_on_statements=frozenset([group_statement]),
            approvers=frozenset(["approver@example.com"]),
        )

        # This should not raise TypeError: unhashable type: 'dict'
        result = decision.dict()

        assert isinstance(result, dict)
        assert result["grant"] is False
        assert result["reason"] == DecisionReason.RequiresApproval.value
        assert "based_on_statements" in result
        assert isinstance(result["based_on_statements"], list)
        assert len(result["based_on_statements"]) == 1

    def test_access_request_decision_with_multiple_statements(self):
        """Test serialization with multiple statements in the frozenset."""
        statements = frozenset(
            [
                Statement.model_validate(
                    {
                        "resource_type": "Account",
                        "resource": ["123456789012"],
                        "permission_set": ["AdminAccess"],
                        "approvers": ["approver1@example.com"],
                        "allow_self_approval": True,
                    }
                ),
                Statement.model_validate(
                    {
                        "resource_type": "Account",
                        "resource": ["987654321098"],
                        "permission_set": ["ReadOnlyAccess"],
                        "approvers": ["approver2@example.com"],
                        "allow_self_approval": False,
                    }
                ),
            ]
        )

        decision = AccessRequestDecision(
            grant=False,
            reason=DecisionReason.RequiresApproval,
            based_on_statements=statements,
            approvers=frozenset(["approver1@example.com", "approver2@example.com"]),
        )

        # This should not raise TypeError: unhashable type: 'dict'
        result = decision.dict()

        assert isinstance(result, dict)
        assert isinstance(result["based_on_statements"], list)
        assert len(result["based_on_statements"]) == 2
        assert isinstance(result["approvers"], list)
        assert len(result["approvers"]) == 2

    def test_statement_dict_serialization(self):
        """Test that Statement.dict() works correctly."""
        statement = Statement.model_validate(
            {
                "resource_type": "Account",
                "resource": ["123456789012", "*"],
                "permission_set": ["AdminAccess", "PowerUserAccess"],
                "approvers": ["approver@example.com", "admin@example.com"],
                "allow_self_approval": True,
                "approval_is_not_required": False,
            }
        )

        result = statement.dict()

        assert isinstance(result, dict)
        assert result["resource_type"] == "Account"
        assert isinstance(result["resource"], list)
        assert isinstance(result["permission_set"], list)
        assert isinstance(result["approvers"], list)
        assert result["allow_self_approval"] is True
        assert result["approval_is_not_required"] is False

    def test_group_statement_dict_serialization(self):
        """Test that GroupStatement.dict() works correctly."""
        group_statement = GroupStatement.model_validate(
            {
                "resource": ["11111111-2222-3333-4444-555555555555"],
                "approvers": ["approver@example.com"],
                "allow_self_approval": False,
                "approval_is_not_required": True,
            }
        )

        result = group_statement.dict()

        assert isinstance(result, dict)
        assert isinstance(result["resource"], list)
        assert isinstance(result["approvers"], list)
        assert result["allow_self_approval"] is False
        assert result["approval_is_not_required"] is True
