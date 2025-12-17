"""Property-based tests for sync state manager.

Tests the correctness of sync action computation using Hypothesis.
"""

from hypothesis import given, settings, strategies as st, assume

from attribute_mapper import AttributeCondition, AttributeMappingRule, AttributeMapper
from sync_state import GroupMembershipState, UserInfo, SyncStateManager


# Strategies for generating test data
attribute_name_strategy = st.sampled_from(["department", "employeeType", "costCenter", "jobTitle", "location", "team"])

# Use simpler attribute values to avoid health check issues
attribute_value_strategy = st.sampled_from(
    [
        "Engineering",
        "Sales",
        "HR",
        "Finance",
        "Marketing",
        "Operations",
        "FullTime",
        "PartTime",
        "Contractor",
        "Intern",
        "CC001",
        "CC002",
        "CC003",
        "CC004",
        "Manager",
        "Engineer",
        "Analyst",
        "Director",
    ]
)

user_id_strategy = st.uuids().map(str)
group_id_strategy = st.uuids().map(str)
email_strategy = st.emails()

group_name_strategy = st.sampled_from(
    [
        "Engineering",
        "Sales",
        "HR",
        "Finance",
        "Marketing",
        "Operations",
        "Admins",
        "Developers",
        "Managers",
        "Analysts",
    ]
)

policy_strategy = st.sampled_from(["warn", "remove"])


@st.composite
def user_info_strategy(draw: st.DrawFn, attributes: dict[str, str] | None = None) -> UserInfo:
    """Generate a UserInfo with optional specific attributes."""
    user_id = draw(user_id_strategy)
    email = draw(email_strategy)
    if attributes is None:
        num_attrs = draw(st.integers(min_value=0, max_value=4))
        attr_names = draw(st.permutations(["department", "employeeType", "costCenter", "jobTitle", "location", "team"]))
        selected_attrs = attr_names[:num_attrs]
        attributes = {name: draw(attribute_value_strategy) for name in selected_attrs}
    return UserInfo(user_id=user_id, email=email, attributes=attributes)


@st.composite
def matching_user_and_rule_strategy(draw: st.DrawFn) -> tuple[UserInfo, AttributeMappingRule, str]:
    """Generate a user that matches a rule, along with the rule and group ID."""
    group_id = draw(group_id_strategy)
    group_name = draw(group_name_strategy)

    # Generate 1-3 conditions with unique attribute names
    num_conditions = draw(st.integers(min_value=1, max_value=3))
    attr_names = draw(st.permutations(["department", "employeeType", "costCenter", "jobTitle"]))
    selected_attrs = attr_names[:num_conditions]

    conditions = []
    user_attributes = {}
    for attr_name in selected_attrs:
        value = draw(attribute_value_strategy)
        conditions.append(AttributeCondition(attribute_name=attr_name, expected_value=value))
        user_attributes[attr_name] = value

    rule = AttributeMappingRule(
        group_name=group_name,
        group_id=group_id,
        conditions=tuple(conditions),
    )

    user = UserInfo(
        user_id=draw(user_id_strategy),
        email=draw(email_strategy),
        attributes=user_attributes,
    )

    return user, rule, group_id


@st.composite
def non_matching_user_and_rule_strategy(draw: st.DrawFn) -> tuple[UserInfo, AttributeMappingRule, str]:
    """Generate a user that does NOT match a rule, along with the rule and group ID."""
    group_id = draw(group_id_strategy)
    group_name = draw(group_name_strategy)

    # Use fixed pairs of different values to avoid slow generation
    value_pairs = [
        ("Engineering", "Sales"),
        ("FullTime", "PartTime"),
        ("CC001", "CC002"),
        ("Manager", "Engineer"),
    ]

    # Generate conditions with one attribute
    attr_name = draw(st.sampled_from(["department", "employeeType", "costCenter", "jobTitle"]))
    pair_idx = ["department", "employeeType", "costCenter", "jobTitle"].index(attr_name)
    rule_value, user_value = value_pairs[pair_idx]

    conditions = [AttributeCondition(attribute_name=attr_name, expected_value=rule_value)]

    rule = AttributeMappingRule(
        group_name=group_name,
        group_id=group_id,
        conditions=tuple(conditions),
    )

    # User has a different value for the attribute
    user_attributes = {attr_name: user_value}

    user = UserInfo(
        user_id=draw(user_id_strategy),
        email=draw(email_strategy),
        attributes=user_attributes,
    )

    return user, rule, group_id


class TestMembershipAdditionIdempotence:
    """
    **Feature: attribute-based-group-sync, Property 4: Membership addition idempotence**
    **Validates: Requirements 1.4**

    For any user who matches a rule but is not a group member, the system should
    add them to the group, and subsequent sync operations should not attempt to
    re-add them.
    """

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_user_matching_rule_not_in_group_generates_add_action(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 4: Membership addition idempotence**
        **Validates: Requirements 1.4**

        For any user who matches a rule but is not currently a member of the group,
        the system should generate an "add" action.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Current state: user is NOT in the group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),  # Empty - user not in group
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have exactly one add action
        add_actions = [a for a in actions if a.action_type == "add"]
        assert len(add_actions) == 1
        assert add_actions[0].user_id == user.user_id
        assert add_actions[0].group_id == group_id

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_user_matching_rule_already_in_group_no_action(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 4: Membership addition idempotence**
        **Validates: Requirements 1.4**

        For any user who matches a rule and is already a member of the group,
        the system should NOT generate any action (idempotence).
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Current state: user IS already in the group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),  # User already in group
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have no actions for this user
        user_actions = [a for a in actions if a.user_id == user.user_id]
        assert len(user_actions) == 0

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_add_action_is_idempotent_across_multiple_syncs(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 4: Membership addition idempotence**
        **Validates: Requirements 1.4**

        Simulating multiple sync operations: after the first sync adds a user,
        subsequent syncs should not generate add actions for that user.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # First sync: user not in group
        state_before_add = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            )
        }

        first_actions = manager.compute_sync_actions([user], state_before_add)
        add_actions_first = [a for a in first_actions if a.action_type == "add"]
        assert len(add_actions_first) == 1

        # Second sync: simulate that the add was executed (user now in group)
        state_after_add = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        second_actions = manager.compute_sync_actions([user], state_after_add)
        add_actions_second = [a for a in second_actions if a.action_type == "add"]

        # No add actions on second sync
        assert len(add_actions_second) == 0

    @settings(max_examples=100)
    @given(
        data=matching_user_and_rule_strategy(),
        other_user_id=user_id_strategy,
    )
    def test_add_action_only_for_matching_users(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        other_user_id: str,
    ):
        """
        **Feature: attribute-based-group-sync, Property 4: Membership addition idempotence**
        **Validates: Requirements 1.4**

        Add actions should only be generated for users who match the rules,
        not for other users already in the group.
        """
        user, rule, group_id = data

        # Ensure other_user_id is different
        assume(other_user_id != user.user_id)

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Current state: another user is in the group (but matching user is not)
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([other_user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have an add action for the matching user
        add_actions = [a for a in actions if a.action_type == "add"]
        assert len(add_actions) == 1
        assert add_actions[0].user_id == user.user_id


class TestMembershipRemovalCorrectness:
    """
    **Feature: attribute-based-group-sync, Property 5: Membership removal correctness**
    **Validates: Requirements 1.5**

    For any user who is a group member but does not match any rules,
    the system should remove them from the group (when policy is "remove").
    """

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_user_not_matching_rule_in_group_generates_remove_action(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 5: Membership removal correctness**
        **Validates: Requirements 1.5**

        For any user who is in a group but does not match the rules,
        when policy is "remove", the system should generate a "remove" action.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="remove",  # Policy is remove
        )

        # Current state: user IS in the group but doesn't match rules
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have exactly one remove action
        remove_actions = [a for a in actions if a.action_type == "remove"]
        assert len(remove_actions) == 1
        assert remove_actions[0].user_id == user.user_id
        assert remove_actions[0].group_id == group_id

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_user_not_matching_rule_in_group_generates_warn_action_when_policy_warn(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 5: Membership removal correctness**
        **Validates: Requirements 1.5**

        For any user who is in a group but does not match the rules,
        when policy is "warn", the system should generate a "warn" action (not remove).
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",  # Policy is warn
        )

        # Current state: user IS in the group but doesn't match rules
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have exactly one warn action (not remove)
        warn_actions = [a for a in actions if a.action_type == "warn"]
        remove_actions = [a for a in actions if a.action_type == "remove"]

        assert len(warn_actions) == 1
        assert len(remove_actions) == 0
        assert warn_actions[0].user_id == user.user_id
        assert warn_actions[0].group_id == group_id

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_user_not_matching_rule_not_in_group_no_action(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 5: Membership removal correctness**
        **Validates: Requirements 1.5**

        For any user who does not match rules and is not in the group,
        no action should be generated.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="remove",
        )

        # Current state: user is NOT in the group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),  # Empty
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have no actions
        user_actions = [a for a in actions if a.user_id == user.user_id]
        assert len(user_actions) == 0

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_removal_is_idempotent_across_multiple_syncs(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 5: Membership removal correctness**
        **Validates: Requirements 1.5**

        After a user is removed, subsequent syncs should not generate
        any actions for that user.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="remove",
        )

        # First sync: user in group but doesn't match
        state_before_remove = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        first_actions = manager.compute_sync_actions([user], state_before_remove)
        remove_actions_first = [a for a in first_actions if a.action_type == "remove"]
        assert len(remove_actions_first) == 1

        # Second sync: simulate that the remove was executed
        state_after_remove = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),  # User removed
            )
        }

        second_actions = manager.compute_sync_actions([user], state_after_remove)

        # No actions on second sync
        user_actions = [a for a in second_actions if a.user_id == user.user_id]
        assert len(user_actions) == 0

    @settings(max_examples=100)
    @given(
        matching_data=matching_user_and_rule_strategy(),
        non_matching_data=non_matching_user_and_rule_strategy(),
    )
    def test_only_non_matching_users_are_removed(
        self,
        matching_data: tuple[UserInfo, AttributeMappingRule, str],
        non_matching_data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 5: Membership removal correctness**
        **Validates: Requirements 1.5**

        Only users who don't match rules should be removed; users who match
        should remain in the group.
        """
        matching_user, rule, group_id = matching_data
        non_matching_user, _, _ = non_matching_data

        # Ensure different user IDs
        assume(matching_user.user_id != non_matching_user.user_id)

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="remove",
        )

        # Both users are in the group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([matching_user.user_id, non_matching_user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([matching_user, non_matching_user], current_state)

        # Matching user should have no actions
        matching_user_actions = [a for a in actions if a.user_id == matching_user.user_id]
        assert len(matching_user_actions) == 0

        # Non-matching user should have a remove action
        non_matching_user_actions = [a for a in actions if a.user_id == non_matching_user.user_id]
        assert len(non_matching_user_actions) == 1
        assert non_matching_user_actions[0].action_type == "remove"


class TestSyncActionProperties:
    """Additional tests for SyncAction properties."""

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_add_action_includes_matched_attributes(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        Add actions should include the attributes that matched the rule.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        add_actions = [a for a in actions if a.action_type == "add"]
        assert len(add_actions) == 1

        action = add_actions[0]
        assert action.matched_attributes is not None

        # Verify matched attributes contain the rule's condition attributes
        for cond in rule.conditions:
            assert cond.attribute_name in action.matched_attributes
            assert action.matched_attributes[cond.attribute_name] == user.attributes[cond.attribute_name]

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_action_includes_user_email(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        All actions should include the user's email for notifications.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        for action in actions:
            assert action.user_email == user.email

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_action_includes_group_name(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        All actions should include the group name for logging/notifications.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        for action in actions:
            assert action.group_name == rule.group_name


class TestManualAssignmentDetection:
    """Tests for manual assignment detection helper methods."""

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_is_manual_assignment_returns_false_for_matching_user(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        is_manual_assignment should return False for users who match rules.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        assert manager.is_manual_assignment(user, group_id) is False

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_is_manual_assignment_returns_true_for_non_matching_user(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        is_manual_assignment should return True for users who don't match rules.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        assert manager.is_manual_assignment(user, group_id) is True

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_get_users_matching_group_includes_matching_users(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        get_users_matching_group should include users who match the group's rules.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        matching_users = manager.get_users_matching_group([user], group_id)

        assert user.user_id in matching_users

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_get_users_matching_group_excludes_non_matching_users(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        get_users_matching_group should exclude users who don't match the group's rules.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        matching_users = manager.get_users_matching_group([user], group_id)

        assert user.user_id not in matching_users
