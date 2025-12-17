"""Property-based tests for sync configuration loading and validation.

Tests the correctness of configuration loading and validation logic using Hypothesis.
"""

import json
import os

import pytest
from hypothesis import given, settings, strategies as st, assume

from sync_config import (
    SyncConfiguration,
    SyncConfigurationError,
    load_sync_config_from_env,
    validate_sync_config,
    resolve_group_names,
    resolve_group_names_from_identity_store,
    get_valid_rules_for_resolved_groups,
)


# Strategies for generating test data
group_name_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), min_codepoint=65, max_codepoint=122),
    min_size=1,
    max_size=30,
)

attribute_name_strategy = st.sampled_from(["department", "employeeType", "costCenter", "jobTitle", "location", "team"])

attribute_value_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=50,
)

group_id_strategy = st.uuids().map(str)

policy_strategy = st.sampled_from(["warn", "remove"])

schedule_strategy = st.sampled_from(
    [
        "rate(1 hour)",
        "rate(30 minutes)",
        "rate(1 day)",
        "cron(0 * * * ? *)",
    ]
)


@st.composite
def valid_mapping_rule_strategy(draw: st.DrawFn, group_names: list[str] | None = None) -> dict:
    """Generate a valid mapping rule dictionary."""
    if group_names:
        group_name = draw(st.sampled_from(group_names))
    else:
        group_name = draw(group_name_strategy)

    num_attrs = draw(st.integers(min_value=1, max_value=4))
    attr_names = draw(st.permutations(["department", "employeeType", "costCenter", "jobTitle", "location", "team"]))
    selected_attrs = attr_names[:num_attrs]

    attributes = {}
    for attr_name in selected_attrs:
        attributes[attr_name] = draw(attribute_value_strategy)

    return {
        "group_name": group_name,
        "attributes": attributes,
    }


@st.composite
def valid_sync_config_env_strategy(draw: st.DrawFn) -> dict[str, str]:
    """Generate valid environment variables for sync configuration."""
    # Generate managed groups
    num_groups = draw(st.integers(min_value=1, max_value=5))
    managed_groups = [draw(group_name_strategy) for _ in range(num_groups)]
    # Ensure unique group names
    managed_groups = list(set(managed_groups))
    assume(len(managed_groups) >= 1)

    # Generate rules that reference managed groups
    num_rules = draw(st.integers(min_value=1, max_value=len(managed_groups)))
    rules = []
    for i in range(num_rules):
        group_name = managed_groups[i % len(managed_groups)]
        rule = draw(valid_mapping_rule_strategy([group_name]))
        rules.append(rule)

    policy = draw(policy_strategy)
    schedule = draw(schedule_strategy)

    return {
        "ATTRIBUTE_SYNC_ENABLED": "true",
        "ATTRIBUTE_SYNC_MANAGED_GROUPS": json.dumps(managed_groups),
        "ATTRIBUTE_SYNC_RULES": json.dumps(rules),
        "ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY": policy,
        "ATTRIBUTE_SYNC_SCHEDULE": schedule,
    }


class TestConfigurationLoadingCorrectness:
    """
    **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
    **Validates: Requirements 1.1, 2.1, 2.2**

    For any valid sync configuration, the system should correctly parse and load
    all mapping rules with their attribute conditions.
    """

    @settings(max_examples=100)
    @given(env_vars=valid_sync_config_env_strategy())
    def test_valid_config_loads_all_rules(self, env_vars: dict[str, str]):
        """
        **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
        **Validates: Requirements 1.1, 2.1, 2.2**

        For any valid configuration, all mapping rules should be correctly parsed
        and loaded with their group names and attribute conditions.
        """
        # Set environment variables
        os.environ.clear()
        os.environ.update(env_vars)

        config = load_sync_config_from_env()

        # Verify enabled flag
        assert config.enabled is True

        # Verify managed groups were loaded
        expected_groups = json.loads(env_vars["ATTRIBUTE_SYNC_MANAGED_GROUPS"])
        assert set(config.managed_group_names) == set(expected_groups)

        # Verify rules were loaded
        expected_rules = json.loads(env_vars["ATTRIBUTE_SYNC_RULES"])
        assert len(config.mapping_rules) == len(expected_rules)

        # Verify each rule has correct structure
        for i, rule in enumerate(config.mapping_rules):
            assert "group_name" in rule
            assert "attributes" in rule
            assert rule["group_name"] == expected_rules[i]["group_name"]
            assert rule["attributes"] == expected_rules[i]["attributes"]

        # Verify policy
        assert config.manual_assignment_policy == env_vars["ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY"]

        # Verify schedule
        assert config.schedule_expression == env_vars["ATTRIBUTE_SYNC_SCHEDULE"]

    @settings(max_examples=100)
    @given(
        managed_groups=st.lists(group_name_strategy, min_size=1, max_size=5, unique=True),
    )
    def test_config_loads_managed_groups_correctly(self, managed_groups: list[str]):
        """
        **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
        **Validates: Requirements 1.1, 2.1, 2.2**

        For any list of managed group names, the configuration should correctly
        parse and store them as a tuple.
        """
        os.environ.clear()
        os.environ["ATTRIBUTE_SYNC_ENABLED"] = "true"
        os.environ["ATTRIBUTE_SYNC_MANAGED_GROUPS"] = json.dumps(managed_groups)
        os.environ["ATTRIBUTE_SYNC_RULES"] = json.dumps([{"group_name": managed_groups[0], "attributes": {"department": "Test"}}])

        config = load_sync_config_from_env()

        assert isinstance(config.managed_group_names, tuple)
        assert set(config.managed_group_names) == set(managed_groups)

    @settings(max_examples=100)
    @given(
        attributes=st.dictionaries(
            keys=attribute_name_strategy,
            values=attribute_value_strategy,
            min_size=1,
            max_size=4,
        ),
    )
    def test_config_loads_attribute_conditions_correctly(self, attributes: dict[str, str]):
        """
        **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
        **Validates: Requirements 1.1, 2.1, 2.2**

        For any set of attribute conditions, the configuration should correctly
        parse and store them in the mapping rules.
        """
        group_name = "TestGroup"
        rule = {"group_name": group_name, "attributes": attributes}

        os.environ.clear()
        os.environ["ATTRIBUTE_SYNC_ENABLED"] = "true"
        os.environ["ATTRIBUTE_SYNC_MANAGED_GROUPS"] = json.dumps([group_name])
        os.environ["ATTRIBUTE_SYNC_RULES"] = json.dumps([rule])

        config = load_sync_config_from_env()

        assert len(config.mapping_rules) == 1
        loaded_rule = config.mapping_rules[0]
        assert loaded_rule["attributes"] == attributes

    @settings(max_examples=50)
    @given(policy=policy_strategy)
    def test_config_loads_policy_correctly(self, policy: str):
        """
        **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
        **Validates: Requirements 1.1, 2.1, 2.2**

        For any valid policy value, the configuration should correctly parse it.
        """
        os.environ.clear()
        os.environ["ATTRIBUTE_SYNC_ENABLED"] = "true"
        os.environ["ATTRIBUTE_SYNC_MANAGED_GROUPS"] = json.dumps(["TestGroup"])
        os.environ["ATTRIBUTE_SYNC_RULES"] = json.dumps([{"group_name": "TestGroup", "attributes": {"department": "Test"}}])
        os.environ["ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY"] = policy

        config = load_sync_config_from_env()

        assert config.manual_assignment_policy == policy

    def test_config_disabled_by_default(self):
        """
        **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
        **Validates: Requirements 1.1, 2.1, 2.2**

        When ATTRIBUTE_SYNC_ENABLED is not set, the feature should be disabled.
        """
        os.environ.clear()

        config = load_sync_config_from_env()

        assert config.enabled is False

    def test_config_defaults_when_disabled(self):
        """
        **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
        **Validates: Requirements 1.1, 2.1, 2.2**

        When disabled, configuration should have sensible defaults.
        """
        os.environ.clear()
        os.environ["ATTRIBUTE_SYNC_ENABLED"] = "false"

        config = load_sync_config_from_env()

        assert config.enabled is False
        assert config.managed_group_names == ()
        assert config.mapping_rules == ()
        assert config.manual_assignment_policy == "warn"
        assert config.schedule_expression == "rate(1 hour)"


class TestConfigurationValidationCompleteness:
    """
    **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
    **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

    For any configuration with missing required fields (managed groups or mapping rules),
    the system should produce validation errors and not proceed with sync.
    """

    @settings(max_examples=100)
    @given(env_vars=valid_sync_config_env_strategy())
    def test_valid_config_passes_validation(self, env_vars: dict[str, str]):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        For any valid configuration, validation should return no errors.
        """
        os.environ.clear()
        os.environ.update(env_vars)

        config = load_sync_config_from_env()
        errors = validate_sync_config(config)

        assert errors == []

    def test_validation_fails_when_enabled_without_managed_groups(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When enabled without managed groups, validation should fail.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=(),
            managed_group_ids={},
            mapping_rules=({"group_name": "Test", "attributes": {"dept": "Eng"}},),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        errors = validate_sync_config(config)

        assert len(errors) >= 1
        assert any("managed_groups" in e.lower() for e in errors)

    def test_validation_fails_when_enabled_without_rules(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When enabled without mapping rules, validation should fail.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=("TestGroup",),
            managed_group_ids={},
            mapping_rules=(),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        errors = validate_sync_config(config)

        assert len(errors) >= 1
        assert any("rules" in e.lower() for e in errors)

    @settings(max_examples=50)
    @given(
        managed_groups=st.lists(group_name_strategy, min_size=1, max_size=3, unique=True),
        unmanaged_group=group_name_strategy,
    )
    def test_validation_fails_when_rule_references_unmanaged_group(self, managed_groups: list[str], unmanaged_group: str):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When a rule references a group not in managed_groups, validation should fail.
        """
        # Ensure unmanaged_group is not in managed_groups
        assume(unmanaged_group not in managed_groups)

        config = SyncConfiguration(
            enabled=True,
            managed_group_names=tuple(managed_groups),
            managed_group_ids={},
            mapping_rules=({"group_name": unmanaged_group, "attributes": {"dept": "Eng"}},),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        errors = validate_sync_config(config)

        assert len(errors) >= 1
        assert any("not in managed_groups" in e for e in errors)

    def test_validation_fails_when_rule_missing_group_name(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When a rule is missing group_name, validation should fail.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=("TestGroup",),
            managed_group_ids={},
            mapping_rules=({"attributes": {"dept": "Eng"}},),  # Missing group_name
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        errors = validate_sync_config(config)

        assert len(errors) >= 1
        assert any("group_name" in e for e in errors)

    def test_validation_fails_when_rule_missing_attributes(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When a rule is missing attributes, validation should fail.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=("TestGroup",),
            managed_group_ids={},
            mapping_rules=({"group_name": "TestGroup"},),  # Missing attributes
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        errors = validate_sync_config(config)

        assert len(errors) >= 1
        assert any("attributes" in e for e in errors)

    def test_validation_skipped_when_disabled(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When disabled, validation should pass even with empty config.
        """
        config = SyncConfiguration(
            enabled=False,
            managed_group_names=(),
            managed_group_ids={},
            mapping_rules=(),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        errors = validate_sync_config(config)

        assert errors == []

    def test_invalid_policy_raises_error(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When an invalid policy is provided, loading should raise an error.
        """
        os.environ.clear()
        os.environ["ATTRIBUTE_SYNC_ENABLED"] = "true"
        os.environ["ATTRIBUTE_SYNC_MANAGED_GROUPS"] = json.dumps(["TestGroup"])
        os.environ["ATTRIBUTE_SYNC_RULES"] = json.dumps([{"group_name": "TestGroup", "attributes": {"department": "Test"}}])
        os.environ["ATTRIBUTE_SYNC_MANUAL_ASSIGNMENT_POLICY"] = "invalid_policy"

        with pytest.raises(SyncConfigurationError) as exc_info:
            load_sync_config_from_env()

        assert "warn" in str(exc_info.value) or "remove" in str(exc_info.value)

    def test_invalid_json_in_managed_groups_raises_error(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When invalid JSON is provided for managed groups, loading should raise an error.
        """
        os.environ.clear()
        os.environ["ATTRIBUTE_SYNC_ENABLED"] = "true"
        os.environ["ATTRIBUTE_SYNC_MANAGED_GROUPS"] = "not valid json"

        with pytest.raises(SyncConfigurationError) as exc_info:
            load_sync_config_from_env()

        assert "JSON" in str(exc_info.value)

    def test_invalid_json_in_rules_raises_error(self):
        """
        **Feature: attribute-based-group-sync, Property 9: Configuration validation completeness**
        **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

        When invalid JSON is provided for rules, loading should raise an error.
        """
        os.environ.clear()
        os.environ["ATTRIBUTE_SYNC_ENABLED"] = "true"
        os.environ["ATTRIBUTE_SYNC_MANAGED_GROUPS"] = json.dumps(["TestGroup"])
        os.environ["ATTRIBUTE_SYNC_RULES"] = "not valid json"

        with pytest.raises(SyncConfigurationError) as exc_info:
            load_sync_config_from_env()

        assert "JSON" in str(exc_info.value)


class TestGroupNameResolution:
    """Tests for group name to ID resolution."""

    @settings(max_examples=100)
    @given(
        group_names=st.lists(group_name_strategy, min_size=1, max_size=5, unique=True),
    )
    def test_resolve_group_names_populates_ids(self, group_names: list[str]):
        """
        For any set of group names with corresponding IDs, resolution should
        populate the managed_group_ids mapping.
        """
        # Create config with group names
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=tuple(group_names),
            managed_group_ids={},
            mapping_rules=tuple({"group_name": name, "attributes": {"dept": "Test"}} for name in group_names),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        # Create name to ID mapping
        name_to_id = {name: f"id-{i}" for i, name in enumerate(group_names)}

        resolved_config = resolve_group_names(config, name_to_id)

        # Verify all IDs were resolved
        assert len(resolved_config.managed_group_ids) == len(group_names)
        for name in group_names:
            assert name in resolved_config.managed_group_ids
            assert resolved_config.managed_group_ids[name] == name_to_id[name]

    def test_resolve_group_names_handles_missing_groups(self):
        """
        When some groups cannot be resolved, the function should still return
        a config with the groups that could be resolved.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=("Group1", "Group2", "Group3"),
            managed_group_ids={},
            mapping_rules=({"group_name": "Group1", "attributes": {"dept": "Test"}},),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        # Only provide ID for Group1
        name_to_id = {"Group1": "id-1"}

        resolved_config = resolve_group_names(config, name_to_id)

        # Only Group1 should be resolved
        assert len(resolved_config.managed_group_ids) == 1
        assert "Group1" in resolved_config.managed_group_ids
        assert "Group2" not in resolved_config.managed_group_ids
        assert "Group3" not in resolved_config.managed_group_ids

    def test_get_group_id_returns_correct_id(self):
        """
        get_group_id should return the correct ID for a resolved group.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=("TestGroup",),
            managed_group_ids={"TestGroup": "test-id-123"},
            mapping_rules=({"group_name": "TestGroup", "attributes": {"dept": "Test"}},),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        assert config.get_group_id("TestGroup") == "test-id-123"
        assert config.get_group_id("NonExistent") is None


class TestInvalidGroupReferenceHandling:
    """
    **Feature: attribute-based-group-sync, Property 8: Invalid group reference handling**
    **Validates: Requirements 2.5**

    For any mapping rule that references a group ID not in the managed groups list,
    the system should log an error and skip that rule without failing other rules.
    """

    @settings(max_examples=100)
    @given(
        existing_groups=st.lists(group_name_strategy, min_size=1, max_size=5, unique=True),
        missing_groups=st.lists(group_name_strategy, min_size=1, max_size=3, unique=True),
    )
    def test_rules_with_missing_groups_are_skipped(self, existing_groups: list[str], missing_groups: list[str]):
        """
        **Feature: attribute-based-group-sync, Property 8: Invalid group reference handling**
        **Validates: Requirements 2.5**

        For any configuration where some rules reference groups that don't exist
        in Identity Store, those rules should be skipped while valid rules are kept.
        """
        # Ensure missing groups are actually missing (not in existing)
        missing_groups = [g for g in missing_groups if g not in existing_groups]
        assume(len(missing_groups) >= 1)

        all_group_names = existing_groups + missing_groups

        # Create rules for all groups (some will be valid, some invalid)
        rules = [{"group_name": name, "attributes": {"department": "Test"}} for name in all_group_names]

        config = SyncConfiguration(
            enabled=True,
            managed_group_names=tuple(all_group_names),
            managed_group_ids={},
            mapping_rules=tuple(rules),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        # Simulate Identity Store only having existing_groups
        name_to_id = {name: f"id-{i}" for i, name in enumerate(existing_groups)}

        # Resolve group names
        resolved_config = resolve_group_names(config, name_to_id)

        # Get valid rules
        valid_rules = get_valid_rules_for_resolved_groups(resolved_config)

        # Verify only rules for existing groups are returned
        assert len(valid_rules) == len(existing_groups)
        valid_group_names = {rule["group_name"] for rule in valid_rules}
        assert valid_group_names == set(existing_groups)

        # Verify missing groups are not in resolved IDs
        for missing_group in missing_groups:
            assert missing_group not in resolved_config.managed_group_ids

    @settings(max_examples=100)
    @given(
        existing_groups=st.lists(group_name_strategy, min_size=2, max_size=5, unique=True),
    )
    def test_valid_rules_are_preserved_when_some_groups_missing(self, existing_groups: list[str]):
        """
        **Feature: attribute-based-group-sync, Property 8: Invalid group reference handling**
        **Validates: Requirements 2.5**

        When some groups are missing, valid rules should still be processed correctly.
        """
        # Split groups: some exist, some don't
        split_point = len(existing_groups) // 2
        groups_in_store = existing_groups[:split_point] if split_point > 0 else existing_groups[:1]
        groups_not_in_store = existing_groups[split_point:] if split_point > 0 else existing_groups[1:]

        assume(len(groups_in_store) >= 1)
        assume(len(groups_not_in_store) >= 1)

        # Create rules for all groups
        rules = [{"group_name": name, "attributes": {"department": f"Dept-{i}"}} for i, name in enumerate(existing_groups)]

        config = SyncConfiguration(
            enabled=True,
            managed_group_names=tuple(existing_groups),
            managed_group_ids={},
            mapping_rules=tuple(rules),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        # Only groups_in_store exist in Identity Store
        name_to_id = {name: f"id-{i}" for i, name in enumerate(groups_in_store)}

        resolved_config = resolve_group_names(config, name_to_id)
        valid_rules = get_valid_rules_for_resolved_groups(resolved_config)

        # Verify valid rules match groups_in_store
        assert len(valid_rules) == len(groups_in_store)
        for rule in valid_rules:
            assert rule["group_name"] in groups_in_store
            # Verify the rule attributes are preserved
            assert "attributes" in rule
            assert "department" in rule["attributes"]

    def test_all_rules_skipped_when_no_groups_exist(self):
        """
        **Feature: attribute-based-group-sync, Property 8: Invalid group reference handling**
        **Validates: Requirements 2.5**

        When no managed groups exist in Identity Store, all rules should be skipped.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=("Group1", "Group2", "Group3"),
            managed_group_ids={},
            mapping_rules=(
                {"group_name": "Group1", "attributes": {"dept": "Eng"}},
                {"group_name": "Group2", "attributes": {"dept": "Sales"}},
                {"group_name": "Group3", "attributes": {"dept": "HR"}},
            ),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        # Empty Identity Store
        name_to_id: dict[str, str] = {}

        resolved_config = resolve_group_names(config, name_to_id)
        valid_rules = get_valid_rules_for_resolved_groups(resolved_config)

        assert len(valid_rules) == 0
        assert len(resolved_config.managed_group_ids) == 0

    def test_all_rules_valid_when_all_groups_exist(self):
        """
        **Feature: attribute-based-group-sync, Property 8: Invalid group reference handling**
        **Validates: Requirements 2.5**

        When all managed groups exist in Identity Store, all rules should be valid.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=("Group1", "Group2"),
            managed_group_ids={},
            mapping_rules=(
                {"group_name": "Group1", "attributes": {"dept": "Eng"}},
                {"group_name": "Group2", "attributes": {"dept": "Sales"}},
            ),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        # All groups exist
        name_to_id = {"Group1": "id-1", "Group2": "id-2"}

        resolved_config = resolve_group_names(config, name_to_id)
        valid_rules = get_valid_rules_for_resolved_groups(resolved_config)

        assert len(valid_rules) == 2
        assert len(resolved_config.managed_group_ids) == 2

    @settings(max_examples=50)
    @given(
        group_names=st.lists(group_name_strategy, min_size=1, max_size=5, unique=True),
    )
    def test_resolve_group_names_from_identity_store_with_cache(self, group_names: list[str]):
        """
        **Feature: attribute-based-group-sync, Property 8: Invalid group reference handling**
        **Validates: Requirements 2.5**

        When cached groups are provided, the function should use them instead of
        querying Identity Store.
        """
        config = SyncConfiguration(
            enabled=True,
            managed_group_names=tuple(group_names),
            managed_group_ids={},
            mapping_rules=tuple({"group_name": name, "attributes": {"dept": "Test"}} for name in group_names),
            manual_assignment_policy="warn",
            schedule_expression="rate(1 hour)",
        )

        # Create cached groups mapping
        cached_groups = {name: f"cached-id-{i}" for i, name in enumerate(group_names)}

        # Use None for identity_store_client since we're using cache
        # This would fail if the function tried to use the client
        resolved_config, returned_cache = resolve_group_names_from_identity_store(
            config=config,
            identity_store_client=None,  # type: ignore[arg-type]
            identity_store_id="test-store-id",
            cached_groups=cached_groups,
        )

        # Verify all groups were resolved from cache
        assert len(resolved_config.managed_group_ids) == len(group_names)
        for name in group_names:
            assert name in resolved_config.managed_group_ids
            assert resolved_config.managed_group_ids[name] == cached_groups[name]

        # Verify returned cache is the same as input cache
        assert returned_cache == cached_groups
