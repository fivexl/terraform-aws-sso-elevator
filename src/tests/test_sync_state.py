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

        # Ensure non_matching_user doesn't accidentally match the rule from matching_data
        assume(not rule.matches(non_matching_user.attributes))

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


class TestManualAssignmentDetectionAccuracy:
    """
    **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
    **Validates: Requirements 3.1, 3.2**

    For any user in a managed group, the system should correctly determine whether
    they were added by sync (matches rules) or manually (doesn't match rules).
    """

    @settings(max_examples=100)
    @given(
        matching_data=matching_user_and_rule_strategy(),
        non_matching_data=non_matching_user_and_rule_strategy(),
    )
    def test_manual_assignment_detection_distinguishes_matching_from_non_matching(
        self,
        matching_data: tuple[UserInfo, AttributeMappingRule, str],
        non_matching_data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
        **Validates: Requirements 3.1, 3.2**

        For any group with both matching and non-matching users as members,
        the system should correctly identify which users are manual assignments.
        """
        matching_user, rule, group_id = matching_data
        non_matching_user, _, _ = non_matching_data

        # Ensure different user IDs
        assume(matching_user.user_id != non_matching_user.user_id)

        # Ensure non_matching_user doesn't accidentally match the rule from matching_data
        assume(not rule.matches(non_matching_user.attributes))

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
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

        # Matching user should NOT be detected as manual assignment (no action)
        matching_user_actions = [a for a in actions if a.user_id == matching_user.user_id]
        assert len(matching_user_actions) == 0, "Matching user should not have any actions"

        # Non-matching user SHOULD be detected as manual assignment (warn action)
        non_matching_user_actions = [a for a in actions if a.user_id == non_matching_user.user_id]
        assert len(non_matching_user_actions) == 1, "Non-matching user should have exactly one action"
        assert non_matching_user_actions[0].action_type == "warn", "Non-matching user should have warn action"

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_manual_assignment_detected_for_user_in_group_not_matching_rules(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
        **Validates: Requirements 3.1, 3.2**

        For any user who is in a managed group but does not match the attribute rules,
        the system should detect them as a manual assignment.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # User is in the group but doesn't match rules
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should detect as manual assignment
        assert len(actions) == 1
        assert actions[0].action_type == "warn"
        assert actions[0].user_id == user.user_id
        assert "manual assignment" in actions[0].reason.lower() or "does not match" in actions[0].reason.lower()

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_sync_added_user_not_detected_as_manual_assignment(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
        **Validates: Requirements 3.1, 3.2**

        For any user who is in a managed group and matches the attribute rules,
        the system should NOT detect them as a manual assignment.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # User is in the group and matches rules (sync-added)
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should NOT detect as manual assignment - no actions
        user_actions = [a for a in actions if a.user_id == user.user_id]
        assert len(user_actions) == 0, "Sync-added user should not be detected as manual assignment"

    @settings(max_examples=100)
    @given(
        data=non_matching_user_and_rule_strategy(),
        policy=policy_strategy,
    )
    def test_manual_assignment_detection_independent_of_policy(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        policy: str,
    ):
        """
        **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
        **Validates: Requirements 3.1, 3.2**

        Manual assignment detection should work correctly regardless of the
        configured policy (warn or remove). The detection itself is independent
        of what action is taken.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy=policy,
        )

        # User is in the group but doesn't match rules
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should detect as manual assignment regardless of policy
        assert len(actions) == 1
        assert actions[0].user_id == user.user_id
        # Action type depends on policy, but detection should happen
        assert actions[0].action_type in ["warn", "remove"]

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_is_manual_assignment_method_accuracy_for_matching_user(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
        **Validates: Requirements 3.1, 3.2**

        The is_manual_assignment helper method should correctly return False
        for users who match the rules for a group.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # User matches rules - should NOT be manual assignment
        is_manual = manager.is_manual_assignment(user, group_id)
        assert is_manual is False, "User matching rules should not be detected as manual assignment"

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_is_manual_assignment_method_accuracy_for_non_matching_user(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
        **Validates: Requirements 3.1, 3.2**

        The is_manual_assignment helper method should correctly return True
        for users who do not match the rules for a group.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # User doesn't match rules - SHOULD be manual assignment
        is_manual = manager.is_manual_assignment(user, group_id)
        assert is_manual is True, "User not matching rules should be detected as manual assignment"

    @settings(max_examples=100)
    @given(
        matching_data=matching_user_and_rule_strategy(),
        non_matching_data=non_matching_user_and_rule_strategy(),
    )
    def test_all_current_members_identified_in_managed_groups(
        self,
        matching_data: tuple[UserInfo, AttributeMappingRule, str],
        non_matching_data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
        **Validates: Requirements 3.1, 3.2**

        For any sync operation, the system should identify ALL users currently
        in managed groups and correctly classify each one.
        """
        matching_user, rule, group_id = matching_data
        non_matching_user, _, _ = non_matching_data

        # Ensure different user IDs
        assume(matching_user.user_id != non_matching_user.user_id)

        # Ensure non_matching_user doesn't accidentally match the rule from matching_data
        assume(not rule.matches(non_matching_user.attributes))

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Both users are current members
        all_members = frozenset([matching_user.user_id, non_matching_user.user_id])

        # Get users matching the group rules
        matching_users = manager.get_users_matching_group([matching_user, non_matching_user], group_id)

        # Verify all current members are accounted for
        # Matching user should be in matching_users
        assert matching_user.user_id in matching_users

        # Non-matching user should NOT be in matching_users
        assert non_matching_user.user_id not in matching_users

        # Manual assignments = current_members - matching_users
        manual_assignments = all_members - matching_users
        assert non_matching_user.user_id in manual_assignments
        assert matching_user.user_id not in manual_assignments


class TestPolicyBasedRemovalBehavior:
    """
    **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
    **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

    For any manual assignment, when policy is "warn" the user should not be removed,
    and when policy is "remove" the user should be removed.
    """

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_warn_policy_generates_warn_action_not_remove(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any manual assignment (user in group but not matching rules),
        when policy is "warn", the system should generate a "warn" action
        and NOT a "remove" action.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # User is in the group but doesn't match rules (manual assignment)
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have exactly one action for this user
        user_actions = [a for a in actions if a.user_id == user.user_id]
        assert len(user_actions) == 1

        # Action should be "warn", not "remove"
        assert user_actions[0].action_type == "warn"
        assert user_actions[0].group_id == group_id

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_remove_policy_generates_remove_action(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any manual assignment (user in group but not matching rules),
        when policy is "remove", the system should generate a "remove" action.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="remove",
        )

        # User is in the group but doesn't match rules (manual assignment)
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have exactly one action for this user
        user_actions = [a for a in actions if a.user_id == user.user_id]
        assert len(user_actions) == 1

        # Action should be "remove"
        assert user_actions[0].action_type == "remove"
        assert user_actions[0].group_id == group_id

    @settings(max_examples=100)
    @given(
        data=non_matching_user_and_rule_strategy(),
        policy=policy_strategy,
    )
    def test_policy_determines_action_type_for_manual_assignments(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        policy: str,
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any manual assignment and any policy setting, the action type
        should match the configured policy.
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy=policy,
        )

        # User is in the group but doesn't match rules (manual assignment)
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Should have exactly one action for this user
        user_actions = [a for a in actions if a.user_id == user.user_id]
        assert len(user_actions) == 1

        # Action type should match policy
        expected_action_type = policy  # "warn" -> "warn", "remove" -> "remove"
        assert user_actions[0].action_type == expected_action_type

    @settings(max_examples=100)
    @given(data=matching_user_and_rule_strategy())
    def test_matching_users_not_affected_by_policy(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any user who matches the rules and is in the group,
        no action should be generated regardless of policy setting.
        """
        user, rule, group_id = data

        # Test with both policies
        for policy in ["warn", "remove"]:
            mapper = AttributeMapper([rule])
            manager = SyncStateManager(
                managed_group_ids={rule.group_name: group_id},
                mapper=mapper,
                manual_assignment_policy=policy,
            )

            # User is in the group AND matches rules (not a manual assignment)
            current_state = {
                group_id: GroupMembershipState(
                    group_id=group_id,
                    group_name=rule.group_name,
                    current_members=frozenset([user.user_id]),
                )
            }

            actions = manager.compute_sync_actions([user], current_state)

            # Should have no actions for matching users
            user_actions = [a for a in actions if a.user_id == user.user_id]
            assert len(user_actions) == 0, f"Matching user should have no actions with policy '{policy}'"

    @settings(max_examples=100)
    @given(
        matching_data=matching_user_and_rule_strategy(),
        non_matching_data=non_matching_user_and_rule_strategy(),
    )
    def test_policy_only_affects_manual_assignments(
        self,
        matching_data: tuple[UserInfo, AttributeMappingRule, str],
        non_matching_data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any group with both matching and non-matching users,
        the policy should only affect the non-matching users (manual assignments).
        """
        matching_user, rule, group_id = matching_data
        non_matching_user, _, _ = non_matching_data

        # Ensure different user IDs
        assume(matching_user.user_id != non_matching_user.user_id)

        # Ensure non_matching_user doesn't accidentally match the rule
        assume(not rule.matches(non_matching_user.attributes))

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

        # Non-matching user should have a remove action (policy is "remove")
        non_matching_user_actions = [a for a in actions if a.user_id == non_matching_user.user_id]
        assert len(non_matching_user_actions) == 1
        assert non_matching_user_actions[0].action_type == "remove"

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_warn_action_includes_manual_assignment_reason(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any manual assignment with "warn" policy, the action reason
        should indicate it's a manual assignment detection.
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
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        assert len(actions) == 1
        # Reason should mention manual assignment or not matching rules
        reason_lower = actions[0].reason.lower()
        assert "manual assignment" in reason_lower or "does not match" in reason_lower

    @settings(max_examples=100)
    @given(data=non_matching_user_and_rule_strategy())
    def test_remove_action_includes_policy_reason(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any manual assignment with "remove" policy, the action reason
        should indicate the policy is "remove".
        """
        user, rule, group_id = data

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="remove",
        )

        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([user.user_id]),
            )
        }

        actions = manager.compute_sync_actions([user], current_state)

        assert len(actions) == 1
        # Reason should mention the policy is "remove"
        reason_lower = actions[0].reason.lower()
        assert "remove" in reason_lower or "policy" in reason_lower

    @settings(max_examples=100)
    @given(
        data=non_matching_user_and_rule_strategy(),
        other_user_ids=st.lists(user_id_strategy, min_size=1, max_size=3),
    )
    def test_policy_applied_consistently_to_all_manual_assignments(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        other_user_ids: list[str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
        **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

        For any group with multiple manual assignments, the policy should
        be applied consistently to all of them.
        """
        user, rule, group_id = data

        # Create additional non-matching users
        non_matching_users = [user]
        for uid in other_user_ids:
            if uid != user.user_id:
                # Create user with attributes that don't match the rule
                non_matching_users.append(
                    UserInfo(
                        user_id=uid,
                        email=f"{uid[:8]}@example.com",
                        attributes={"department": "NonMatching"},
                    )
                )

        # Ensure none of the users match the rule
        for u in non_matching_users:
            assume(not rule.matches(u.attributes))

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="remove",
        )

        # All non-matching users are in the group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(u.user_id for u in non_matching_users),
            )
        }

        actions = manager.compute_sync_actions(non_matching_users, current_state)

        # All users should have remove actions
        for u in non_matching_users:
            user_actions = [a for a in actions if a.user_id == u.user_id]
            assert len(user_actions) == 1, f"User {u.user_id} should have exactly one action"
            assert user_actions[0].action_type == "remove", f"User {u.user_id} should have remove action"


class TestManagedGroupIsolation:
    """
    **Feature: attribute-based-group-sync, Property 6: Managed group isolation**
    **Validates: Requirements 10.4**

    For any group not in the managed groups list, the system should perform
    no operations (no adds, removes, or monitoring).
    """

    @settings(max_examples=100)
    @given(
        data=matching_user_and_rule_strategy(),
        non_managed_group_id=group_id_strategy,
        non_managed_group_name=group_name_strategy,
    )
    def test_non_managed_group_receives_no_add_actions(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        non_managed_group_id: str,
        non_managed_group_name: str,
    ):
        """
        **Feature: attribute-based-group-sync, Property 6: Managed group isolation**
        **Validates: Requirements 10.4**

        For any group not in the managed groups list, even if users match rules
        for that group, no add actions should be generated.
        """
        user, rule, managed_group_id = data

        # Ensure non-managed group is different from managed group
        assume(non_managed_group_id != managed_group_id)
        assume(non_managed_group_name != rule.group_name)

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: managed_group_id},  # Only managed group
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Include both managed and non-managed groups in current state
        current_state = {
            managed_group_id: GroupMembershipState(
                group_id=managed_group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            ),
            non_managed_group_id: GroupMembershipState(
                group_id=non_managed_group_id,
                group_name=non_managed_group_name,
                current_members=frozenset(),
            ),
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Actions should only be for the managed group
        non_managed_actions = [a for a in actions if a.group_id == non_managed_group_id]
        assert len(non_managed_actions) == 0, "Non-managed group should receive no actions"

        # Managed group should still receive appropriate actions
        managed_actions = [a for a in actions if a.group_id == managed_group_id]
        assert len(managed_actions) == 1, "Managed group should receive add action"
        assert managed_actions[0].action_type == "add"

    @settings(max_examples=100)
    @given(
        data=non_matching_user_and_rule_strategy(),
        non_managed_group_id=group_id_strategy,
        non_managed_group_name=group_name_strategy,
    )
    def test_non_managed_group_receives_no_remove_or_warn_actions(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        non_managed_group_id: str,
        non_managed_group_name: str,
    ):
        """
        **Feature: attribute-based-group-sync, Property 6: Managed group isolation**
        **Validates: Requirements 10.4**

        For any group not in the managed groups list, even if users in that group
        don't match rules, no remove or warn actions should be generated.
        """
        user, rule, managed_group_id = data

        # Ensure non-managed group is different from managed group
        assume(non_managed_group_id != managed_group_id)
        assume(non_managed_group_name != rule.group_name)

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: managed_group_id},  # Only managed group
            mapper=mapper,
            manual_assignment_policy="remove",  # Even with remove policy
        )

        # User is in the non-managed group (but not in managed group)
        current_state = {
            managed_group_id: GroupMembershipState(
                group_id=managed_group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            ),
            non_managed_group_id: GroupMembershipState(
                group_id=non_managed_group_id,
                group_name=non_managed_group_name,
                current_members=frozenset([user.user_id]),  # User in non-managed group
            ),
        }

        actions = manager.compute_sync_actions([user], current_state)

        # No actions should be generated for the non-managed group
        non_managed_actions = [a for a in actions if a.group_id == non_managed_group_id]
        assert len(non_managed_actions) == 0, "Non-managed group should receive no actions"

    @settings(max_examples=100)
    @given(
        user=user_info_strategy(),
        non_managed_group_ids=st.lists(group_id_strategy, min_size=1, max_size=5, unique=True),
    )
    def test_completely_non_managed_state_produces_no_actions(
        self,
        user: UserInfo,
        non_managed_group_ids: list[str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 6: Managed group isolation**
        **Validates: Requirements 10.4**

        When the current state contains only non-managed groups, no actions
        should be generated regardless of user attributes or group membership.
        """
        # Create a rule for a managed group that doesn't exist in current state
        managed_group_id = "managed-group-id-not-in-state"
        managed_group_name = "ManagedGroup"

        # Ensure managed group ID is not in non_managed_group_ids
        assume(managed_group_id not in non_managed_group_ids)

        rule = AttributeMappingRule(
            group_name=managed_group_name,
            group_id=managed_group_id,
            conditions=(AttributeCondition(attribute_name="department", expected_value="Engineering"),),
        )

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={managed_group_name: managed_group_id},
            mapper=mapper,
            manual_assignment_policy="remove",
        )

        # Current state contains only non-managed groups with user as member
        current_state = {
            gid: GroupMembershipState(
                group_id=gid,
                group_name=f"NonManaged_{i}",
                current_members=frozenset([user.user_id]),
            )
            for i, gid in enumerate(non_managed_group_ids)
        }

        actions = manager.compute_sync_actions([user], current_state)

        # No actions should be generated for any non-managed group
        assert len(actions) == 0, "No actions should be generated when only non-managed groups exist in state"

    @settings(max_examples=100)
    @given(
        matching_data=matching_user_and_rule_strategy(),
        non_managed_group_ids=st.lists(group_id_strategy, min_size=1, max_size=3, unique=True),
    )
    def test_managed_group_actions_not_affected_by_non_managed_groups(
        self,
        matching_data: tuple[UserInfo, AttributeMappingRule, str],
        non_managed_group_ids: list[str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 6: Managed group isolation**
        **Validates: Requirements 10.4**

        The presence of non-managed groups in the current state should not
        affect the actions generated for managed groups.
        """
        user, rule, managed_group_id = matching_data

        # Ensure non-managed group IDs are different from managed group
        assume(managed_group_id not in non_managed_group_ids)

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: managed_group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # State with only managed group
        state_managed_only = {
            managed_group_id: GroupMembershipState(
                group_id=managed_group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            ),
        }

        # State with managed group + non-managed groups
        state_with_non_managed = {
            managed_group_id: GroupMembershipState(
                group_id=managed_group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            ),
            **{
                gid: GroupMembershipState(
                    group_id=gid,
                    group_name=f"NonManaged_{i}",
                    current_members=frozenset([user.user_id]),  # User in non-managed groups
                )
                for i, gid in enumerate(non_managed_group_ids)
            },
        }

        actions_managed_only = manager.compute_sync_actions([user], state_managed_only)
        actions_with_non_managed = manager.compute_sync_actions([user], state_with_non_managed)

        # Filter to only managed group actions
        managed_actions_only = [a for a in actions_managed_only if a.group_id == managed_group_id]
        managed_actions_with = [a for a in actions_with_non_managed if a.group_id == managed_group_id]

        # Actions for managed group should be the same regardless of non-managed groups
        assert len(managed_actions_only) == len(managed_actions_with)
        for a1, a2 in zip(managed_actions_only, managed_actions_with, strict=False):
            assert a1.action_type == a2.action_type
            assert a1.user_id == a2.user_id
            assert a1.group_id == a2.group_id


class TestManagedGroupProcessingCompleteness:
    """
    **Feature: attribute-based-group-sync, Property 7: Managed group processing completeness**
    **Validates: Requirements 10.2, 10.3**

    For any group in the managed groups list, the system should evaluate all users
    against rules and monitor for manual assignments.
    """

    @settings(max_examples=100)
    @given(
        matching_data=matching_user_and_rule_strategy(),
        additional_users=st.lists(user_info_strategy(), min_size=1, max_size=5),
    )
    def test_all_users_evaluated_against_managed_group_rules(
        self,
        matching_data: tuple[UserInfo, AttributeMappingRule, str],
        additional_users: list[UserInfo],
    ):
        """
        **Feature: attribute-based-group-sync, Property 7: Managed group processing completeness**
        **Validates: Requirements 10.2**

        For any managed group, all users should be evaluated against the rules
        to determine if they should be added.
        """
        matching_user, rule, group_id = matching_data

        # Ensure unique user IDs
        all_user_ids = {matching_user.user_id} | {u.user_id for u in additional_users}
        assume(len(all_user_ids) == 1 + len(additional_users))

        all_users = [matching_user] + additional_users

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Empty group - all matching users should get add actions
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            ),
        }

        actions = manager.compute_sync_actions(all_users, current_state)

        # Count how many users actually match the rule
        matching_users = [u for u in all_users if rule.matches(u.attributes)]

        # Each matching user should have an add action
        add_actions = [a for a in actions if a.action_type == "add"]
        add_user_ids = {a.user_id for a in add_actions}

        for user in matching_users:
            assert user.user_id in add_user_ids, f"Matching user {user.user_id} should have add action"

    @settings(max_examples=100)
    @given(
        data=non_matching_user_and_rule_strategy(),
        additional_non_matching_users=st.lists(user_info_strategy(), min_size=1, max_size=3),
    )
    def test_all_manual_assignments_detected_in_managed_group(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        additional_non_matching_users: list[UserInfo],
    ):
        """
        **Feature: attribute-based-group-sync, Property 7: Managed group processing completeness**
        **Validates: Requirements 10.3**

        For any managed group, all users who are members but don't match rules
        should be detected as manual assignments.
        """
        user, rule, group_id = data

        # Ensure unique user IDs and none match the rule
        all_non_matching = [user] + [
            u for u in additional_non_matching_users if u.user_id != user.user_id and not rule.matches(u.attributes)
        ]

        # Need at least the original user
        assume(len(all_non_matching) >= 1)

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # All non-matching users are in the group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(u.user_id for u in all_non_matching),
            ),
        }

        actions = manager.compute_sync_actions(all_non_matching, current_state)

        # Each non-matching user should have a warn action
        warn_actions = [a for a in actions if a.action_type == "warn"]
        warn_user_ids = {a.user_id for a in warn_actions}

        for user in all_non_matching:
            assert user.user_id in warn_user_ids, f"Non-matching user {user.user_id} should be detected as manual assignment"

    @settings(max_examples=100)
    @given(
        managed_groups=st.lists(
            st.tuples(group_name_strategy, group_id_strategy),
            min_size=2,
            max_size=4,
            unique_by=lambda x: x[1],  # Unique by group_id
        ),
        user=user_info_strategy(),
    )
    def test_all_managed_groups_processed(
        self,
        managed_groups: list[tuple[str, str]],
        user: UserInfo,
    ):
        """
        **Feature: attribute-based-group-sync, Property 7: Managed group processing completeness**
        **Validates: Requirements 10.2, 10.3**

        For any set of managed groups, all of them should be processed
        during a sync operation.
        """
        # Ensure unique group names
        group_names = [g[0] for g in managed_groups]
        assume(len(set(group_names)) == len(group_names))

        # Create rules for each managed group with different attribute requirements
        attr_values = ["Engineering", "Sales", "HR", "Finance", "Marketing"]
        rules = []
        for i, (group_name, group_id) in enumerate(managed_groups):
            rules.append(
                AttributeMappingRule(
                    group_name=group_name,
                    group_id=group_id,
                    conditions=(
                        AttributeCondition(
                            attribute_name="department",
                            expected_value=attr_values[i % len(attr_values)],
                        ),
                    ),
                )
            )

        managed_group_ids = dict(managed_groups)

        mapper = AttributeMapper(rules)
        manager = SyncStateManager(
            managed_group_ids=managed_group_ids,
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # User is in all managed groups
        current_state = {
            gid: GroupMembershipState(
                group_id=gid,
                group_name=name,
                current_members=frozenset([user.user_id]),
            )
            for name, gid in managed_groups
        }

        actions = manager.compute_sync_actions([user], current_state)

        # Each managed group should have been processed
        # (user will either match and have no action, or not match and have warn action)
        processed_group_ids = set()
        for action in actions:
            processed_group_ids.add(action.group_id)

        # For groups where user doesn't match, there should be a warn action
        for name, gid in managed_groups:
            rule = next(r for r in rules if r.group_id == gid)
            if not rule.matches(user.attributes):
                assert gid in processed_group_ids, f"Managed group {name} should be processed"

    @settings(max_examples=100)
    @given(
        matching_data=matching_user_and_rule_strategy(),
        non_matching_data=non_matching_user_and_rule_strategy(),
    )
    def test_managed_group_handles_mixed_membership(
        self,
        matching_data: tuple[UserInfo, AttributeMappingRule, str],
        non_matching_data: tuple[UserInfo, AttributeMappingRule, str],
    ):
        """
        **Feature: attribute-based-group-sync, Property 7: Managed group processing completeness**
        **Validates: Requirements 10.2, 10.3**

        For any managed group with mixed membership (some matching, some not),
        the system should correctly handle both cases.
        """
        matching_user, rule, group_id = matching_data
        non_matching_user, _, _ = non_matching_data

        # Ensure different user IDs
        assume(matching_user.user_id != non_matching_user.user_id)

        # Ensure non_matching_user doesn't match the rule
        assume(not rule.matches(non_matching_user.attributes))

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Both users are in the group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset([matching_user.user_id, non_matching_user.user_id]),
            ),
        }

        actions = manager.compute_sync_actions([matching_user, non_matching_user], current_state)

        # Matching user should have no actions (already correctly in group)
        matching_actions = [a for a in actions if a.user_id == matching_user.user_id]
        assert len(matching_actions) == 0, "Matching user should have no actions"

        # Non-matching user should have warn action (manual assignment)
        non_matching_actions = [a for a in actions if a.user_id == non_matching_user.user_id]
        assert len(non_matching_actions) == 1, "Non-matching user should have one action"
        assert non_matching_actions[0].action_type == "warn", "Non-matching user should have warn action"

    @settings(max_examples=100)
    @given(
        data=matching_user_and_rule_strategy(),
        users_not_in_group=st.lists(user_info_strategy(), min_size=1, max_size=3),
    )
    def test_managed_group_adds_all_matching_users_not_in_group(
        self,
        data: tuple[UserInfo, AttributeMappingRule, str],
        users_not_in_group: list[UserInfo],
    ):
        """
        **Feature: attribute-based-group-sync, Property 7: Managed group processing completeness**
        **Validates: Requirements 10.2**

        For any managed group, all users who match rules but are not in the group
        should receive add actions.
        """
        matching_user, rule, group_id = data

        # Create additional users that match the rule
        matching_users = [matching_user]
        for u in users_not_in_group:
            if u.user_id != matching_user.user_id:
                # Give them attributes that match the rule
                matching_attrs = {cond.attribute_name: cond.expected_value for cond in rule.conditions}
                matching_users.append(
                    UserInfo(
                        user_id=u.user_id,
                        email=u.email,
                        attributes=matching_attrs,
                    )
                )

        mapper = AttributeMapper([rule])
        manager = SyncStateManager(
            managed_group_ids={rule.group_name: group_id},
            mapper=mapper,
            manual_assignment_policy="warn",
        )

        # Empty group
        current_state = {
            group_id: GroupMembershipState(
                group_id=group_id,
                group_name=rule.group_name,
                current_members=frozenset(),
            ),
        }

        actions = manager.compute_sync_actions(matching_users, current_state)

        # All matching users should have add actions
        add_actions = [a for a in actions if a.action_type == "add"]
        add_user_ids = {a.user_id for a in add_actions}

        for user in matching_users:
            assert user.user_id in add_user_ids, f"Matching user {user.user_id} should have add action"
