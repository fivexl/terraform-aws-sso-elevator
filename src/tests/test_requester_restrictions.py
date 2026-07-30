from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import access_control
import config
import entities
import sso
from access_control import (
    DecisionReason,
    make_decision_on_access_request,
    make_decision_on_approve_request,
)
from statement import GroupStatement, Statement, requester_allowed

# ruff: noqa: ANN201, ANN001

GROUP_A = "11111111-2222-3333-4444-555555555555"
GROUP_B = "66666666-7777-8888-9999-000000000000"


def account_statement(**overrides) -> Statement:
    return Statement.model_validate(
        {
            "resource_type": "Account",
            "resource": ["111111111111"],
            "permission_set": ["AdministratorAccess"],
            "approvers": ["approver@test.com"],
        }
        | overrides
    )


def group_statement(**overrides) -> GroupStatement:
    return GroupStatement.model_validate(
        {
            "resource": [GROUP_A],
            "approvers": ["approver@test.com"],
        }
        | overrides
    )


class TestRequesterAllowed:
    def test_unrestricted_statement_allows_anyone(self):
        st = account_statement()
        assert requester_allowed(st, frozenset(["anybody@test.com"]), frozenset()) is True

    def test_allowed_users_match(self):
        st = Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})
        assert requester_allowed(st, frozenset(["dev@test.com"]), frozenset()) is True

    def test_allowed_users_match_is_case_insensitive(self):
        st = Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["Dev@Test.com"]})
        assert requester_allowed(st, frozenset(["dev@test.com"]), frozenset()) is True

    def test_allowed_users_no_match(self):
        st = Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})
        assert requester_allowed(st, frozenset(["other@test.com"]), frozenset()) is False

    def test_allowed_groups_match(self):
        st = Statement.model_validate({**account_statement().model_dump(), "allowed_groups": [GROUP_A]})
        assert requester_allowed(st, frozenset(["dev@test.com"]), frozenset([GROUP_A, GROUP_B])) is True

    def test_allowed_groups_no_match(self):
        st = Statement.model_validate({**account_statement().model_dump(), "allowed_groups": [GROUP_A]})
        assert requester_allowed(st, frozenset(["dev@test.com"]), frozenset([GROUP_B])) is False

    def test_either_users_or_groups_restriction_suffices(self):
        st = Statement.model_validate({**account_statement().model_dump(), "allowed_groups": [GROUP_A], "allowed_users": ["dev@test.com"]})
        assert requester_allowed(st, frozenset(["dev@test.com"]), frozenset()) is True
        assert requester_allowed(st, frozenset(["other@test.com"]), frozenset([GROUP_A])) is True
        assert requester_allowed(st, frozenset(["other@test.com"]), frozenset([GROUP_B])) is False

    def test_group_statement_restrictions(self):
        st = GroupStatement.model_validate({**group_statement().model_dump(), "allowed_groups": [GROUP_B]})
        assert requester_allowed(st, frozenset(["dev@test.com"]), frozenset([GROUP_B])) is True
        assert requester_allowed(st, frozenset(["dev@test.com"]), frozenset()) is False


class TestConfigParsing:
    def test_parse_statement_with_restrictions(self):
        st = config.parse_statement(
            {
                "ResourceType": "Account",
                "Resource": ["111111111111"],
                "PermissionSet": "AdministratorAccess",
                "Approvers": "approver@test.com",
                "AllowedGroups": [GROUP_A],
                "AllowedUsers": ["dev@test.com", "dev2@test.com"],
            }
        )
        assert st.allowed_groups == frozenset([GROUP_A])
        assert st.allowed_users == frozenset(["dev@test.com", "dev2@test.com"])

    def test_parse_statement_accepts_single_string_values(self):
        st = config.parse_statement(
            {
                "ResourceType": "Account",
                "Resource": ["111111111111"],
                "PermissionSet": "AdministratorAccess",
                "AllowedGroups": GROUP_A,
                "AllowedUsers": "dev@test.com",
            }
        )
        assert st.allowed_groups == frozenset([GROUP_A])
        assert st.allowed_users == frozenset(["dev@test.com"])

    def test_parse_statement_defaults_to_unrestricted(self):
        st = config.parse_statement(
            {
                "ResourceType": "Account",
                "Resource": ["111111111111"],
                "PermissionSet": "AdministratorAccess",
            }
        )
        assert st.allowed_groups == frozenset()
        assert st.allowed_users == frozenset()

    def test_parse_group_statement_with_restrictions(self):
        st = config.parse_group_statement(
            {
                "Resource": [GROUP_A],
                "Approvers": "approver@test.com",
                "AllowedGroups": [GROUP_B],
                "AllowedUsers": "dev@test.com",
            }
        )
        assert st.allowed_groups == frozenset([GROUP_B])
        assert st.allowed_users == frozenset(["dev@test.com"])

    def test_parse_group_statement_defaults_to_unrestricted(self):
        st = config.parse_group_statement({"Resource": [GROUP_A]})
        assert st.allowed_groups == frozenset()
        assert st.allowed_users == frozenset()

    def test_parse_statement_rejects_invalid_allowed_group_id(self):
        with pytest.raises(ValidationError):
            config.parse_statement(
                {
                    "ResourceType": "Account",
                    "Resource": ["111111111111"],
                    "PermissionSet": "AdministratorAccess",
                    "AllowedGroups": ["not-a-group-id"],
                }
            )

    def test_parse_statement_rejects_invalid_allowed_user_email(self):
        with pytest.raises(ValidationError):
            config.parse_statement(
                {
                    "ResourceType": "Account",
                    "Resource": ["111111111111"],
                    "PermissionSet": "AdministratorAccess",
                    "AllowedUsers": ["not-an-email"],
                }
            )


class TestAccessRequestDecision:
    def test_requester_in_allowed_group_requires_approval(self):
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_groups": [GROUP_A]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="dev@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
            requester_group_ids=frozenset([GROUP_A]),
        )
        assert decision.grant is False
        assert decision.reason == DecisionReason.RequiresApproval
        assert decision.approvers == frozenset(["approver@test.com"])

    def test_requester_not_in_allowed_group_is_denied(self):
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_groups": [GROUP_A]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="dev@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
            requester_group_ids=frozenset([GROUP_B]),
        )
        assert decision.grant is False
        assert decision.reason == DecisionReason.RequesterNotAllowed
        assert decision.based_on_statements == frozenset()

    def test_requester_in_allowed_users_requires_approval(self):
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="dev@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
        )
        assert decision.reason == DecisionReason.RequiresApproval

    def test_requester_not_in_allowed_users_is_denied(self):
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="other@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
        )
        assert decision.reason == DecisionReason.RequesterNotAllowed

    def test_restriction_blocks_even_when_approval_not_required(self):
        statements = frozenset(
            [
                Statement.model_validate(
                    {
                        **account_statement().model_dump(),
                        "approval_is_not_required": True,
                        "allowed_users": ["dev@test.com"],
                    }
                )
            ]
        )
        decision = make_decision_on_access_request(
            statements,
            requester_email="other@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
        )
        assert decision.grant is False
        assert decision.reason == DecisionReason.RequesterNotAllowed

    def test_unrestricted_statement_still_applies_when_restricted_one_is_filtered(self):
        restricted = Statement.model_validate(
            {**account_statement().model_dump(), "approvers": ["admin@test.com"], "allowed_groups": [GROUP_A]}
        )
        unrestricted = account_statement()
        decision = make_decision_on_access_request(
            frozenset([restricted, unrestricted]),
            requester_email="dev@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
            requester_group_ids=frozenset(),
        )
        assert decision.reason == DecisionReason.RequiresApproval
        assert decision.based_on_statements == frozenset([unrestricted])
        assert decision.approvers == frozenset(["approver@test.com"])

    def test_no_statements_still_reported_when_nothing_affects_request(self):
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="dev@test.com",
            account_id="222222222222",
            permission_set_name="AdministratorAccess",
        )
        assert decision.reason == DecisionReason.NoStatements

    def test_group_request_requester_in_allowed_group(self):
        statements = frozenset([GroupStatement.model_validate({**group_statement().model_dump(), "allowed_groups": [GROUP_B]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="dev@test.com",
            group_id=GROUP_A,
            requester_group_ids=frozenset([GROUP_B]),
        )
        assert decision.reason == DecisionReason.RequiresApproval

    def test_group_request_requester_not_allowed(self):
        statements = frozenset([GroupStatement.model_validate({**group_statement().model_dump(), "allowed_groups": [GROUP_B]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="dev@test.com",
            group_id=GROUP_A,
            requester_group_ids=frozenset(),
        )
        assert decision.reason == DecisionReason.RequesterNotAllowed

    def test_group_request_requester_in_allowed_users(self):
        statements = frozenset([GroupStatement.model_validate({**group_statement().model_dump(), "allowed_users": ["dev@test.com"]})])
        decision = make_decision_on_access_request(
            statements,
            requester_email="dev@test.com",
            group_id=GROUP_A,
        )
        assert decision.reason == DecisionReason.RequiresApproval


class TestApproveRequestDecision:
    def test_approver_cannot_approve_for_ineligible_requester(self):
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})])
        decision = make_decision_on_approve_request(
            action=entities.ApproverAction.Approve,
            statements=statements,
            approver_email="approver@test.com",
            requester_email="other@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
        )
        assert decision.permit is False
        assert decision.grant is False

    def test_approver_can_approve_for_eligible_requester(self):
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})])
        decision = make_decision_on_approve_request(
            action=entities.ApproverAction.Approve,
            statements=statements,
            approver_email="approver@test.com",
            requester_email="dev@test.com",
            account_id="111111111111",
            permission_set_name="AdministratorAccess",
        )
        assert decision.permit is True
        assert decision.grant is True

    def test_group_approval_denied_for_requester_outside_allowed_group(self):
        statements = frozenset([GroupStatement.model_validate({**group_statement().model_dump(), "allowed_groups": [GROUP_B]})])
        decision = make_decision_on_approve_request(
            action=entities.ApproverAction.Approve,
            statements=statements,  # type: ignore # noqa: PGH003
            approver_email="approver@test.com",
            requester_email="dev@test.com",
            group_id=GROUP_A,
            requester_group_ids=frozenset(),
        )
        assert decision.permit is False

    def test_group_approval_permitted_for_requester_in_allowed_group(self):
        statements = frozenset([GroupStatement.model_validate({**group_statement().model_dump(), "allowed_groups": [GROUP_B]})])
        decision = make_decision_on_approve_request(
            action=entities.ApproverAction.Approve,
            statements=statements,  # type: ignore # noqa: PGH003
            approver_email="approver@test.com",
            requester_email="dev@test.com",
            group_id=GROUP_A,
            requester_group_ids=frozenset([GROUP_B]),
        )
        assert decision.permit is True

class TestRequesterGroupResolution:
    def test_email_variants_include_secondary_fallback_domains(self):
        # conftest sets secondary_fallback_email_domains to ["domen.com"]
        variants = access_control.requester_email_variants("Dev@Test.com")
        assert variants == frozenset(["dev@test.com", "devdomen.com"])

    def test_email_variants_empty_email(self):
        assert access_control.requester_email_variants("") == frozenset()

    def test_group_lookup_skipped_when_no_statement_restricts_by_group(self, monkeypatch):
        def fail(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("get_requester_group_ids should not be called")

        monkeypatch.setattr(access_control, "get_requester_group_ids", fail)
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_users": ["dev@test.com"]})])
        assert access_control.get_requester_group_ids_if_needed(statements, "dev@test.com") == frozenset()

    def test_group_lookup_performed_when_statement_restricts_by_group(self, monkeypatch):
        monkeypatch.setattr(access_control, "get_requester_group_ids", lambda _email: frozenset([GROUP_A]))
        statements = frozenset([Statement.model_validate({**account_statement().model_dump(), "allowed_groups": [GROUP_A]})])
        assert access_control.get_requester_group_ids_if_needed(statements, "dev@test.com") == frozenset([GROUP_A])

    def test_get_requester_group_ids_propagates_lookup_failure(self, monkeypatch):
        def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("SSO unavailable")

        monkeypatch.setattr(access_control.sso, "describe_sso_instance", boom)
        with pytest.raises(RuntimeError, match="SSO unavailable"):
            access_control.get_requester_group_ids("dev@test.com")

    def test_get_requester_group_ids_returns_confirmed_empty_membership(self, monkeypatch):
        monkeypatch.setattr(access_control.sso, "describe_sso_instance", lambda *_args: MagicMock(identity_store_id="d-123"))
        monkeypatch.setattr(access_control.sso, "get_user_principal_id_by_email", lambda **_kwargs: ("user-id", False))
        monkeypatch.setattr(access_control.sso, "list_groups_for_user", lambda *_args: frozenset())

        assert access_control.get_requester_group_ids("dev@test.com") == frozenset()


class TestListGroupsForUser:
    def test_collects_group_ids_across_pages(self):
        client = MagicMock()
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"GroupMemberships": [{"GroupId": GROUP_A}, {"GroupId": GROUP_B}]},
            {"GroupMemberships": [{"GroupId": GROUP_B}, {"MembershipId": "no-group-id"}]},
        ]

        result = sso.list_groups_for_user("d-1234567890", "user-id", client)

        assert result == frozenset([GROUP_A, GROUP_B])
        client.get_paginator.assert_called_once_with("list_group_memberships_for_member")
        paginator.paginate.assert_called_once_with(IdentityStoreId="d-1234567890", MemberId={"UserId": "user-id"})


class TestModalOptionFiltering:
    """Filtering of the Slack request modal's options to what the requester may request."""

    def test_eligible_group_ids_unrestricted_returns_all(self):
        statements = frozenset([group_statement(), group_statement(resource=[GROUP_B])])
        assert access_control.eligible_group_ids(statements, "dev@test.com", frozenset()) == frozenset([GROUP_A, GROUP_B])

    def test_eligible_group_ids_hides_restricted_groups(self):
        restricted = GroupStatement.model_validate(
            {**group_statement(resource=[GROUP_B]).model_dump(), "allowed_users": ["oncall@test.com"]}
        )
        statements = frozenset([group_statement(), restricted])
        assert access_control.eligible_group_ids(statements, "dev@test.com", frozenset()) == frozenset([GROUP_A])
        assert access_control.eligible_group_ids(statements, "oncall@test.com", frozenset()) == frozenset([GROUP_A, GROUP_B])

    def test_eligible_group_ids_by_group_membership(self):
        restricted = GroupStatement.model_validate({**group_statement(resource=[GROUP_B]).model_dump(), "allowed_groups": [GROUP_A]})
        statements = frozenset([restricted])
        assert access_control.eligible_group_ids(statements, "dev@test.com", frozenset([GROUP_A])) == frozenset([GROUP_B])
        assert access_control.eligible_group_ids(statements, "dev@test.com", frozenset()) == frozenset()

    def test_eligible_accounts_and_permission_sets_wildcard_means_unrestricted(self):
        statements = frozenset([account_statement(resource=["*"], permission_set=["*"])])
        accounts, permission_sets = access_control.eligible_accounts_and_permission_sets(statements, "dev@test.com", frozenset())
        assert accounts is None
        assert permission_sets is None

    def test_eligible_accounts_and_permission_sets_ignores_ineligible_statements(self):
        restricted = Statement.model_validate(
            {
                **account_statement(resource=["222222222222"], permission_set=["AdministratorAccess"]).model_dump(),
                "allowed_users": ["oncall@test.com"],
            }
        )
        unrestricted = account_statement(resource=["111111111111"], permission_set=["ReadOnlyAccess"])
        statements = frozenset([restricted, unrestricted])

        accounts, permission_sets = access_control.eligible_accounts_and_permission_sets(statements, "dev@test.com", frozenset())
        assert accounts == frozenset(["111111111111"])
        assert permission_sets == frozenset(["ReadOnlyAccess"])

        accounts, permission_sets = access_control.eligible_accounts_and_permission_sets(statements, "oncall@test.com", frozenset())
        assert accounts == frozenset(["111111111111", "222222222222"])
        assert permission_sets == frozenset(["ReadOnlyAccess", "AdministratorAccess"])

    def test_filter_account_request_options(self):
        restricted = Statement.model_validate(
            {
                **account_statement(resource=["222222222222"], permission_set=["AdministratorAccess"]).model_dump(),
                "allowed_users": ["oncall@test.com"],
            }
        )
        unrestricted = account_statement(resource=["111111111111"], permission_set=["ReadOnlyAccess"])
        statements = frozenset([restricted, unrestricted])
        all_accounts = [
            entities.aws.Account(id="111111111111", name="dev"),
            entities.aws.Account(id="222222222222", name="prod"),
        ]
        all_permission_sets = [
            entities.aws.PermissionSet(name="ReadOnlyAccess", arn="arn:ro", description=None),
            entities.aws.PermissionSet(name="AdministratorAccess", arn="arn:admin", description=None),
        ]

        accounts, permission_sets = access_control.filter_account_request_options(
            all_accounts, all_permission_sets, statements, "dev@test.com", frozenset()
        )
        assert [account.id for account in accounts] == ["111111111111"]
        assert [permission_set.name for permission_set in permission_sets] == ["ReadOnlyAccess"]

        accounts, permission_sets = access_control.filter_account_request_options(
            all_accounts, all_permission_sets, statements, "oncall@test.com", frozenset()
        )
        assert [account.id for account in accounts] == ["111111111111", "222222222222"]
        assert [permission_set.name for permission_set in permission_sets] == ["ReadOnlyAccess", "AdministratorAccess"]

    def test_filter_account_request_options_wildcard_keeps_everything(self):
        statements = frozenset([account_statement(resource=["*"], permission_set=["*"])])
        all_accounts = [entities.aws.Account(id="111111111111", name="dev")]
        all_permission_sets = [entities.aws.PermissionSet(name="ReadOnlyAccess", arn="arn:ro", description=None)]
        accounts, permission_sets = access_control.filter_account_request_options(
            all_accounts, all_permission_sets, statements, "dev@test.com", frozenset()
        )
        assert accounts == all_accounts
        assert permission_sets == all_permission_sets

    def test_no_available_options_views_have_no_submit_button(self):
        import slack_helpers

        for view_class in (slack_helpers.RequestForAccessView, slack_helpers.RequestForGroupAccessView):
            view = view_class.build_no_available_options_view("You are not allowed to request access.")
            assert view.submit is None
            assert view.callback_id == view_class.CALLBACK_ID
            assert "not allowed" in view.blocks[0].text.text
