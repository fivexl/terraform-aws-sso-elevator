"""Property-based tests for attribute mapping engine.

Tests the correctness of attribute matching logic using Hypothesis.
"""

from hypothesis import given, settings, strategies as st

from attribute_mapper import AttributeCondition, AttributeMappingRule, AttributeMapper


# Strategies for generating test data
attribute_name_strategy = st.sampled_from(["department", "employeeType", "costCenter", "jobTitle", "location", "team"])

attribute_value_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S"), min_codepoint=32, max_codepoint=126), min_size=1, max_size=50
)

user_attributes_strategy = st.dictionaries(keys=attribute_name_strategy, values=attribute_value_strategy, min_size=0, max_size=6)

condition_strategy = st.builds(
    AttributeCondition,
    attribute_name=attribute_name_strategy,
    expected_value=attribute_value_strategy,
)

group_id_strategy = st.uuids().map(str)

rule_strategy = st.builds(
    AttributeMappingRule,
    group_name=st.text(min_size=1, max_size=30),
    group_id=group_id_strategy,
    conditions=st.lists(condition_strategy, min_size=1, max_size=4).map(tuple),
)


class TestAttributeConditionMatching:
    """Tests for AttributeCondition.matches()."""

    @settings(max_examples=100)
    @given(
        attribute_name=attribute_name_strategy,
        expected_value=attribute_value_strategy,
    )
    def test_condition_matches_when_attribute_equals_expected(self, attribute_name: str, expected_value: str):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any attribute condition, when the user has the attribute with the
        exact expected value, the condition should match.
        """
        condition = AttributeCondition(
            attribute_name=attribute_name,
            expected_value=expected_value,
        )
        user_attributes = {attribute_name: expected_value}

        assert condition.matches(user_attributes) is True

    @settings(max_examples=100)
    @given(
        attribute_name=attribute_name_strategy,
        expected_value=attribute_value_strategy,
        actual_value=attribute_value_strategy,
    )
    def test_condition_does_not_match_when_values_differ(self, attribute_name: str, expected_value: str, actual_value: str):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any attribute condition, when the user has a different value
        for the attribute, the condition should not match.
        """
        # Skip when values happen to be equal
        if expected_value == actual_value:
            return

        condition = AttributeCondition(
            attribute_name=attribute_name,
            expected_value=expected_value,
        )
        user_attributes = {attribute_name: actual_value}

        assert condition.matches(user_attributes) is False

    @settings(max_examples=100)
    @given(
        attribute_name=attribute_name_strategy,
        expected_value=attribute_value_strategy,
        user_attributes=user_attributes_strategy,
    )
    def test_condition_does_not_match_when_attribute_missing(
        self, attribute_name: str, expected_value: str, user_attributes: dict[str, str]
    ):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any attribute condition, when the user does not have the
        required attribute, the condition should not match.
        """
        # Remove the attribute if present
        user_attributes = {k: v for k, v in user_attributes.items() if k != attribute_name}

        condition = AttributeCondition(
            attribute_name=attribute_name,
            expected_value=expected_value,
        )

        assert condition.matches(user_attributes) is False


@st.composite
def unique_conditions_strategy(draw: st.DrawFn) -> tuple[AttributeCondition, ...]:
    """Generate conditions with unique attribute names."""
    # Draw a subset of attribute names to ensure uniqueness
    available_attrs = ["department", "employeeType", "costCenter", "jobTitle", "location", "team"]
    num_conditions = draw(st.integers(min_value=1, max_value=min(4, len(available_attrs))))
    selected_attrs = draw(st.permutations(available_attrs).map(lambda x: list(x)[:num_conditions]))

    conditions = []
    for attr_name in selected_attrs:
        value = draw(attribute_value_strategy)
        conditions.append(AttributeCondition(attribute_name=attr_name, expected_value=value))

    return tuple(conditions)


class TestAttributeMappingRuleMatching:
    """Tests for AttributeMappingRule.matches() with AND logic."""

    @settings(max_examples=100)
    @given(
        group_name=st.text(min_size=1, max_size=30),
        group_id=group_id_strategy,
        conditions=unique_conditions_strategy(),
    )
    def test_rule_matches_when_all_conditions_satisfied(self, group_name: str, group_id: str, conditions: tuple[AttributeCondition, ...]):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any mapping rule with multiple conditions (with unique attribute names),
        when the user satisfies ALL conditions, the rule should match (AND logic).
        """
        rule = AttributeMappingRule(
            group_name=group_name,
            group_id=group_id,
            conditions=conditions,
        )

        # Create user attributes that satisfy all conditions
        user_attributes = {cond.attribute_name: cond.expected_value for cond in conditions}

        assert rule.matches(user_attributes) is True

    @settings(max_examples=100)
    @given(
        group_name=st.text(min_size=1, max_size=30),
        group_id=group_id_strategy,
        conditions=unique_conditions_strategy().filter(lambda c: len(c) >= 2),
        failing_index=st.integers(min_value=0),
    )
    def test_rule_does_not_match_when_any_condition_fails(
        self, group_name: str, group_id: str, conditions: tuple[AttributeCondition, ...], failing_index: int
    ):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any mapping rule with multiple conditions (with unique attribute names),
        when the user fails ANY condition, the rule should not match (AND logic).
        """
        rule = AttributeMappingRule(
            group_name=group_name,
            group_id=group_id,
            conditions=conditions,
        )

        # Create user attributes that satisfy all conditions
        user_attributes = {cond.attribute_name: cond.expected_value for cond in conditions}

        # Make one condition fail by removing that attribute
        failing_idx = failing_index % len(conditions)
        failing_attr = conditions[failing_idx].attribute_name
        del user_attributes[failing_attr]

        assert rule.matches(user_attributes) is False

    @settings(max_examples=100)
    @given(
        group_name=st.text(min_size=1, max_size=30),
        group_id=group_id_strategy,
    )
    def test_rule_with_no_conditions_does_not_match(self, group_name: str, group_id: str):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        A rule with no conditions should never match any user.
        """
        rule = AttributeMappingRule(
            group_name=group_name,
            group_id=group_id,
            conditions=(),
        )

        # Even with attributes, empty conditions should not match
        user_attributes = {"department": "Engineering"}

        assert rule.matches(user_attributes) is False


class TestAttributeMapper:
    """Tests for AttributeMapper class."""

    @settings(max_examples=100)
    @given(
        rules=st.lists(rule_strategy, min_size=1, max_size=5),
    )
    def test_mapper_returns_matching_groups(self, rules: list[AttributeMappingRule]):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any set of rules and user attributes, the mapper should return
        exactly the groups where the user matches all conditions.
        """
        mapper = AttributeMapper(rules)

        # Create user attributes that satisfy all conditions of all rules
        user_attributes: dict[str, str] = {}
        for rule in rules:
            for cond in rule.conditions:
                user_attributes[cond.attribute_name] = cond.expected_value

        target_groups = mapper.get_target_groups_for_user(user_attributes)

        # Verify each rule's match status
        for rule in rules:
            if rule.matches(user_attributes):
                assert rule.group_id in target_groups
            else:
                assert rule.group_id not in target_groups

    @settings(max_examples=100)
    @given(
        rules=st.lists(rule_strategy, min_size=1, max_size=5),
    )
    def test_mapper_get_rule_for_group_returns_correct_rule(self, rules: list[AttributeMappingRule]):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any rule in the mapper, get_rule_for_group should return that rule.
        """
        mapper = AttributeMapper(rules)

        for rule in rules:
            retrieved_rule = mapper.get_rule_for_group(rule.group_id)
            # Note: If there are duplicate group_ids, the last one wins
            assert retrieved_rule is not None
            assert retrieved_rule.group_id == rule.group_id

    @settings(max_examples=100)
    @given(
        rules=st.lists(rule_strategy, min_size=0, max_size=5),
        unknown_group_id=group_id_strategy,
    )
    def test_mapper_get_rule_for_unknown_group_returns_none(self, rules: list[AttributeMappingRule], unknown_group_id: str):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        For any group ID not in the rules, get_rule_for_group should return None.
        """
        # Ensure the unknown_group_id is not in any rule
        existing_ids = {rule.group_id for rule in rules}
        if unknown_group_id in existing_ids:
            return  # Skip this case

        mapper = AttributeMapper(rules)

        assert mapper.get_rule_for_group(unknown_group_id) is None

    @settings(max_examples=100)
    @given(user_attributes=user_attributes_strategy)
    def test_mapper_with_no_rules_returns_empty_set(self, user_attributes: dict[str, str]):
        """
        **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
        **Validates: Requirements 1.3, 2.3, 2.4**

        A mapper with no rules should return an empty set for any user.
        """
        mapper = AttributeMapper([])

        target_groups = mapper.get_target_groups_for_user(user_attributes)

        assert target_groups == set()
