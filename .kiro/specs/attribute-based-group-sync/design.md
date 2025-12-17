# Design Document: Attribute-Based Group Sync

## Overview

This feature adds automatic user-to-group synchronization based on IAM Identity Center user attributes. The system will:

1. Read attribute mapping rules from configuration
2. Query users and their attributes from the Identity Store
3. Evaluate users against mapping rules
4. Add/remove users from groups based on attribute matches
5. Detect and handle manually-added users according to policy
6. Log all operations to the audit bucket
7. Send notifications to Slack for important events

The feature is implemented as a new Lambda function (`attribute-syncer`) that runs on a configurable schedule. It integrates with the existing SSO Elevator infrastructure (S3 audit logging, Slack notifications, caching) and operates independently of the request/approval workflow.

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────────────┐
│                         EventBridge Schedule                     │
│                    (e.g., rate(1 hour))                         │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ triggers
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Lambda: attribute-syncer                       │
│                                                                  │
│  1. Load configuration (managed groups + mapping rules)         │
│  2. Query Identity Store for users + attributes                 │
│  3. Query current group memberships for managed groups          │
│  4. Evaluate users against mapping rules                        │
│  5. Add users to groups (if match + not member)                 │
│  6. Detect manual assignments (member but no match)             │
│  7. Remove manual assignments (if policy = remove)              │
│  8. Log all operations to S3                                    │
│  9. Send Slack notifications                                    │
└──────┬──────────────┬──────────────┬──────────────┬────────────┘
       │              │              │              │
       │              │              │              │
       ▼              ▼              ▼              ▼
┌──────────┐  ┌──────────────┐  ┌─────────┐  ┌──────────┐
│ Identity │  │  S3 Bucket   │  │  Slack  │  │ S3 Cache │
│  Store   │  │ (Audit Logs) │  │ Channel │  │  Bucket  │
└──────────┘  └──────────────┘  └─────────┘  └──────────┘
```

### Integration Points

1. **Identity Store Client**: Query users, attributes, groups, and memberships
2. **S3 Audit Bucket**: Write audit entries for all sync operations (reuses existing bucket)
3. **S3 Cache Bucket**: Cache user and group data to minimize API calls (reuses existing bucket)
4. **Slack Client**: Send notifications for manual assignments and errors
5. **EventBridge**: Schedule periodic sync operations

### Configuration Structure

The feature adds new Terraform variables:

```hcl
# Enable/disable the feature
attribute_sync_enabled = true

# List of groups to manage (required if enabled) - using group names
attribute_sync_managed_groups = [
  "Engineering",
  "Finance",
]

# Attribute mapping rules - using group names
attribute_sync_rules = [
  {
    group_name = "Engineering"
    attributes = {
      department = "Engineering"
      employeeType = "FullTime"
    }
  },
  {
    group_name = "Finance"
    attributes = {
      department = "Finance"
    }
  }
]

# Policy for handling manual assignments
attribute_sync_manual_assignment_policy = "warn"  # or "remove"

# Sync schedule
attribute_sync_schedule = "rate(1 hour)"
```

**Group Name Resolution:**
- Configuration uses human-readable group names
- Lambda function resolves names to IDs at runtime by querying Identity Store
- Resolution results are cached to minimize API calls
- If a group name cannot be resolved, an error is logged and that group is skipped

## Components and Interfaces

### 1. Attribute Syncer Lambda Function

**File**: `src/attribute_syncer.py`

**Purpose**: Main entry point for the sync operation

**Key Functions**:
- `lambda_handler(event, context)`: Entry point, orchestrates the sync process
- `perform_sync()`: Main sync logic
- `load_sync_configuration()`: Load managed groups and mapping rules from config
- `resolve_group_names_to_ids()`: Query Identity Store to resolve group names to IDs (with caching)
- `get_users_with_attributes()`: Query all users and their attributes from Identity Store
- `evaluate_user_against_rules()`: Check if a user matches any mapping rules
- `sync_group_membership()`: Add/remove users from a group based on rules
- `detect_manual_assignments()`: Find users in groups who don't match rules
- `handle_manual_assignment()`: Warn or remove based on policy

### 2. Attribute Mapping Engine

**File**: `src/attribute_mapper.py`

**Purpose**: Evaluate users against attribute mapping rules

**Key Classes**:

```python
@dataclass
class AttributeCondition:
    """Single attribute condition (e.g., department = "Engineering")"""
    attribute_name: str
    expected_value: str
    
    def matches(self, user_attributes: dict) -> bool:
        """Check if user attributes satisfy this condition"""
        pass

@dataclass
class AttributeMappingRule:
    """Complete mapping rule for a group"""
    group_name: str
    group_id: str  # Resolved at runtime
    conditions: list[AttributeCondition]
    
    def matches(self, user_attributes: dict) -> bool:
        """Check if user matches ALL conditions (AND logic)"""
        pass

class AttributeMapper:
    """Evaluates users against mapping rules"""
    
    def __init__(self, rules: list[AttributeMappingRule]):
        self.rules = rules
    
    def get_target_groups_for_user(self, user_attributes: dict) -> set[str]:
        """Return set of group IDs the user should belong to"""
        pass
    
    def get_rule_for_group(self, group_id: str) -> AttributeMappingRule | None:
        """Get the mapping rule for a specific group"""
        pass
```

### 3. Sync State Manager

**File**: `src/sync_state.py`

**Purpose**: Track current vs desired state and compute changes

**Key Classes**:

```python
@dataclass
class GroupMembershipState:
    """Current state of a group's membership"""
    group_id: str
    group_name: str
    current_members: set[str]  # user principal IDs
    
@dataclass
class SyncAction:
    """An action to take during sync"""
    action_type: Literal["add", "remove", "warn"]
    user_principal_id: str
    group_id: str
    reason: str
    
class SyncStateMa nager:
    """Computes required sync actions"""
    
    def __init__(
        self,
        managed_groups: list[str],
        mapper: AttributeMapper,
        manual_assignment_policy: str
    ):
        pass
    
    def compute_sync_actions(
        self,
        users: list[User],
        current_state: dict[str, GroupMembershipState]
    ) -> list[SyncAction]:
        """Compute all actions needed to reach desired state"""
        pass
```

### 4. Audit Logger Extension

**File**: `src/s3.py` (extend existing)

**New Audit Entry Types**:

```python
# Add to existing AuditEntry class
audit_entry_type: Literal["account", "group", "sync_add", "sync_remove", "manual_detected"]

# New fields for sync operations
sync_operation: Optional[str] = None  # "attribute_sync"
matched_attributes: Optional[dict] = None  # Attributes that triggered the match
sso_user_email: Optional[str] = None  # Human-readable email for the SSO user
```

**Note**: The existing audit entries already include `requester_email` and `approver_email`. For attribute sync operations, we'll also populate `sso_user_email` with the email address of the user being added/removed for better human readability in audit logs.

### 5. Configuration Loader

**File**: `src/sync_config.py`

**Purpose**: Load and validate sync configuration

**Key Functions**:

```python
@dataclass
class SyncConfiguration:
    """Complete sync configuration"""
    enabled: bool
    managed_group_names: list[str]  # Group names from config
    managed_group_ids: dict[str, str]  # Resolved name -> ID mapping
    mapping_rules: list[AttributeMappingRule]
    manual_assignment_policy: Literal["warn", "remove"]
    schedule_expression: str
    
def load_sync_config() -> SyncConfiguration:
    """Load sync configuration from environment/S3"""
    pass

def resolve_group_names(
    group_names: list[str],
    identity_store_client: IdentityStoreClient,
    identity_store_id: str
) -> dict[str, str]:
    """Resolve group names to IDs, returns name -> ID mapping"""
    pass

def validate_sync_config(config: SyncConfiguration) -> list[str]:
    """Validate configuration and return list of errors"""
    pass
```

## Data Models

### User with Attributes

```python
@dataclass
class UserWithAttributes:
    """User with their Identity Store attributes"""
    user_id: str  # Principal ID
    username: str
    email: str
    attributes: dict[str, str]  # All SCIM attributes
    
    # Common attributes (for convenience)
    @property
    def department(self) -> str | None:
        return self.attributes.get("department")
    
    @property
    def employee_type(self) -> str | None:
        return self.attributes.get("employeeType")
    
    @property
    def cost_center(self) -> str | None:
        return self.attributes.get("costCenter")
```

### Sync Operation Result

```python
@dataclass
class SyncOperationResult:
    """Result of a sync operation"""
    start_time: datetime
    end_time: datetime
    success: bool
    
    # Statistics
    users_evaluated: int
    groups_processed: int
    users_added: int
    users_removed: int
    manual_assignments_detected: int
    manual_assignments_removed: int
    errors: list[str]
    
    def to_slack_message(self) -> str:
        """Format as Slack notification"""
        pass
```

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system-essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Acceptance Criteria Testing Prework

1.1 WHEN the attribute sync feature is enabled THEN the system SHALL read user attribute mapping rules from configuration
Thoughts: This is about system initialization and configuration loading. We can test that given a valid configuration, the system correctly parses and loads the mapping rules. This is testable across different configurations.
Testable: yes - property

1.2 WHEN a sync operation runs THEN the system SHALL query all users from the Identity Store with their attributes
Thoughts: This is about the system's ability to retrieve data. We can mock the Identity Store and verify that the system makes the correct API calls and processes the responses. This should work for any set of users.
Testable: yes - property

1.3 WHEN evaluating a user against mapping rules THEN the system SHALL match user attributes against all configured rules
Thoughts: This is the core matching logic. For any user with attributes and any set of rules, we should be able to verify that the matching logic correctly identifies which rules apply. This is a pure function that can be tested across many inputs.
Testable: yes - property

1.4 WHEN a user matches a rule THEN the system SHALL add the user to the corresponding group if not already a member
Thoughts: This is about the action taken when a match is found. For any user-group pair where the user matches but isn't a member, the system should add them. This is testable as a property.
Testable: yes - property

1.5 WHEN a user no longer matches a rule THEN the system SHALL remove the user from the corresponding group
Thoughts: This is about cleanup when conditions change. For any user who is a member but doesn't match the rules, they should be removed. This is testable as a property.
Testable: yes - property

2.1 WHEN defining a mapping rule THEN the system SHALL accept a group ID as the target resource from the managed groups list
Thoughts: This is about configuration validation. We can test that the system correctly validates group IDs against the managed list for various configurations.
Testable: yes - property

2.2 WHEN defining a mapping rule THEN the system SHALL accept one or more attribute conditions (attribute name and expected value)
Thoughts: This is about configuration parsing. We can test that various attribute condition formats are correctly parsed.
Testable: yes - property

2.3 WHEN defining a mapping rule THEN the system SHALL support exact string matching for attribute values
Thoughts: This is about the matching algorithm. For any attribute value and expected value, exact string matching should work correctly. This is a property of the matching function.
Testable: yes - property

2.4 WHEN defining a mapping rule THEN the system SHALL support multiple attribute conditions with AND logic
Thoughts: This is about boolean logic in matching. For any set of conditions, the AND logic should be correctly applied. This is testable as a property.
Testable: yes - property

2.5 WHEN a mapping rule references a group not in the managed list THEN the system SHALL log an error and skip that rule
Thoughts: This is about error handling for invalid configuration. We can test that for any rule with an invalid group ID, the system logs and skips it.
Testable: yes - property

3.1 WHEN a sync operation runs THEN the system SHALL identify all users currently in managed groups
Thoughts: This is about querying current state. For any set of managed groups, the system should correctly retrieve all current members. This is testable as a property.
Testable: yes - property

3.2 WHEN a user is in a managed group THEN the system SHALL verify if the user was added by the sync system or manually
Thoughts: This requires tracking metadata about how users were added. We need to check if we can distinguish sync-added vs manually-added users. This might require additional data storage or heuristics.
Testable: yes - property

3.3 WHEN a user was manually added and does not match attribute rules THEN the system SHALL log a warning with user and group details
Thoughts: This is about logging behavior. For any manually-added user who doesn't match rules, a warning should be logged. This is testable as a property.
Testable: yes - property

3.4 WHEN a user was manually added and does not match attribute rules THEN the system SHALL send a notification to the Slack channel
Thoughts: This is about notification behavior. For any manually-added user who doesn't match rules, a Slack notification should be sent. This is testable as a property.
Testable: yes - property

3.5 WHEN the removal policy is set to automatic THEN the system SHALL remove manually-added users who don't match rules
Thoughts: This is about policy enforcement. For any manually-added user who doesn't match rules, when policy is "remove", they should be removed. This is testable as a property.
Testable: yes - property

4.1 WHEN configuring the feature THEN the system SHALL accept a policy setting for handling manual assignments
Thoughts: This is about configuration parsing. We can test that various policy values are correctly parsed and validated.
Testable: yes - property

4.2 WHEN the policy is set to "warn" THEN the system SHALL only log and notify about manual assignments without removing them
Thoughts: This is about policy behavior. For any manual assignment when policy is "warn", no removal should occur. This is testable as a property.
Testable: yes - property

4.3 WHEN the policy is set to "remove" THEN the system SHALL automatically remove manual assignments that don't match rules
Thoughts: This is about policy behavior. For any manual assignment when policy is "remove", removal should occur. This is testable as a property.
Testable: yes - property

4.4 WHEN removing a manual assignment THEN the system SHALL log the removal operation to the audit bucket
Thoughts: This is about audit logging. For any removal operation, an audit entry should be written. This is testable as a property.
Testable: yes - property

4.5 WHEN removing a manual assignment THEN the system SHALL send a notification to Slack with user and group details
Thoughts: This is about notification behavior. For any removal, a Slack notification should be sent. This is testable as a property.
Testable: yes - property

5.1 WHEN deploying the feature THEN the system SHALL accept a schedule expression for sync frequency
Thoughts: This is about Terraform configuration. We can test that various schedule expressions are correctly accepted and configured.
Testable: yes - example

5.2 WHEN the schedule triggers THEN the system SHALL invoke the sync Lambda function
Thoughts: This is about EventBridge integration. We can test that the schedule correctly triggers the Lambda. This is more of an integration test.
Testable: yes - example

5.3 WHEN a sync operation starts THEN the system SHALL log the start time and operation type
Thoughts: This is about logging behavior. For any sync operation, start logging should occur. This is testable as a property.
Testable: yes - property

5.4 WHEN a sync operation completes THEN the system SHALL log the completion time and summary statistics
Thoughts: This is about logging behavior. For any sync operation, completion logging should occur. This is testable as a property.
Testable: yes - property

5.5 WHEN a sync operation fails THEN the system SHALL log the error and send a notification to Slack
Thoughts: This is about error handling. For any failure, logging and notification should occur. This is testable as a property.
Testable: yes - property

6.1 WHEN a user is added to a group via sync THEN the system SHALL write an audit entry with operation type "sync_add"
Thoughts: This is about audit logging. For any sync add operation, an audit entry should be written. This is testable as a property.
Testable: yes - property

6.2 WHEN a user is added to a group via sync THEN the system SHALL send a Slack notification with user and group details
Thoughts: This is about notification behavior. For any sync add operation, a Slack notification should be sent. This is testable as a property.
Testable: yes - property

6.3 WHEN a user is removed from a group via sync THEN the system SHALL write an audit entry with operation type "sync_remove"
Thoughts: This is about audit logging. For any sync remove operation, an audit entry should be written. This is testable as a property.
Testable: yes - property

6.4 WHEN a manual assignment is detected THEN the system SHALL write an audit entry with operation type "manual_detected"
Thoughts: This is about audit logging. For any manual assignment detection, an audit entry should be written. This is testable as a property.
Testable: yes - property

6.5 WHEN writing audit entries THEN the system SHALL include user ID, user email, group ID, attribute values, and timestamp
Thoughts: This is about audit entry content. For any audit entry, it should contain all required fields including human-readable email. This is testable as a property.
Testable: yes - property

6.6 WHEN writing audit entries THEN the system SHALL use the same S3 bucket and partitioning scheme as existing audit logs
Thoughts: This is about integration with existing infrastructure. We can test that audit entries are written to the correct location with correct partitioning.
Testable: yes - property

7.1 WHEN deploying the module THEN the attribute sync feature SHALL be disabled by default
Thoughts: This is about default Terraform configuration. This is a specific example to test.
Testable: yes - example

7.2 WHEN the feature is disabled THEN the system SHALL NOT create the sync Lambda function
Thoughts: This is about Terraform conditional resource creation. This is a specific example to test.
Testable: yes - example

7.3 WHEN the feature is disabled THEN the system SHALL NOT create the sync schedule
Thoughts: This is about Terraform conditional resource creation. This is a specific example to test.
Testable: yes - example

7.4 WHEN enabling the feature THEN the system SHALL require explicit configuration of attribute mapping rules
Thoughts: This is about Terraform validation. We can test that enabling without rules causes an error.
Testable: yes - example

7.5 WHEN enabling the feature THEN the system SHALL validate that at least one mapping rule is provided
Thoughts: This is about configuration validation. We can test that empty rules cause a validation error.
Testable: yes - example

8.1 WHEN the Identity Store API is unavailable THEN the system SHALL log the error and retry on the next scheduled run
Thoughts: This is about error handling and resilience. For any API failure, the system should handle it gracefully. This is testable as a property.
Testable: yes - property

8.2 WHEN a group in the configuration does not exist THEN the system SHALL log an error and continue processing other groups
Thoughts: This is about error handling. For any non-existent group, the system should log and continue. This is testable as a property.
Testable: yes - property

8.3 WHEN adding a user to a group fails THEN the system SHALL log the error and continue processing other users
Thoughts: This is about error handling. For any add failure, the system should log and continue. This is testable as a property.
Testable: yes - property

8.4 WHEN removing a user from a group fails THEN the system SHALL log the error and continue processing other users
Thoughts: This is about error handling. For any remove failure, the system should log and continue. This is testable as a property.
Testable: yes - property

8.5 WHEN the sync operation encounters errors THEN the system SHALL send a summary notification to Slack with error count
Thoughts: This is about error reporting. For any sync with errors, a summary should be sent. This is testable as a property.
Testable: yes - property

9.1 WHEN retrieving user lists THEN the system SHALL use cached data if available and not expired
Thoughts: This is about caching behavior. For any cache hit with valid TTL, cached data should be used. This is testable as a property.
Testable: yes - property

9.2 WHEN retrieving group information THEN the system SHALL use cached data if available and not expired
Thoughts: This is about caching behavior. For any cache hit with valid TTL, cached data should be used. This is testable as a property.
Testable: yes - property

9.3 WHEN cache is unavailable THEN the system SHALL fall back to direct API calls
Thoughts: This is about cache fallback. For any cache miss or error, the system should fall back to API. This is testable as a property.
Testable: yes - property

9.4 WHEN sync completes successfully THEN the system SHALL update the cache with fresh data
Thoughts: This is about cache updates. For any successful sync, the cache should be updated. This is testable as a property.
Testable: yes - property

9.5 WHEN cache is enabled in configuration THEN the system SHALL respect the cache TTL settings
Thoughts: This is about cache configuration. For any TTL setting, the system should respect it. This is testable as a property.
Testable: yes - property

10.1 WHEN configuring the feature THEN the system SHALL require an explicit list of managed group IDs
Thoughts: This is about configuration validation. We can test that missing managed groups causes an error.
Testable: yes - example

10.2 WHEN a group is in the managed list THEN the system SHALL add or remove users from that group based on attribute rules
Thoughts: This is about the core sync behavior. For any managed group, sync operations should occur. This is testable as a property.
Testable: yes - property

10.3 WHEN a group is in the managed list THEN the system SHALL monitor that group for manual assignments
Thoughts: This is about monitoring behavior. For any managed group, manual assignment detection should occur. This is testable as a property.
Testable: yes - property

10.4 WHEN a group is NOT in the managed list THEN the system SHALL completely ignore that group
Thoughts: This is about exclusion behavior. For any non-managed group, no operations should occur. This is testable as a property.
Testable: yes - property

10.5 WHEN no managed groups are specified THEN the system SHALL log an error and not perform any sync operations
Thoughts: This is about validation. For empty managed groups, the system should error. This is testable as an example.
Testable: yes - example

## Property Reflection

After reviewing all properties, I've identified the following consolidations:

**Redundancy Analysis:**

1. **Audit Logging Properties (6.1, 6.2, 6.3)** - These three properties all test that audit entries are written for different operation types. They can be consolidated into a single property: "For any sync operation (add, remove, or detect), an audit entry with the correct operation type should be written."

2. **Policy Behavior Properties (4.2, 4.3)** - These test opposite behaviors of the same policy setting. They can be consolidated into: "For any manual assignment, the system behavior (warn-only vs remove) should match the configured policy."

3. **Error Handling Properties (8.1, 8.2, 8.3, 8.4)** - These all test that errors are logged and processing continues. They can be consolidated into: "For any error during sync (API failure, missing group, add failure, remove failure), the system should log the error and continue processing remaining items."

4. **Caching Properties (9.1, 9.2)** - These test the same caching behavior for different resource types. They can be consolidated into: "For any cached resource (users or groups), if cache is valid and not expired, cached data should be used."

5. **Notification Properties (3.4, 4.5)** - These both test Slack notifications for manual assignments. They can be consolidated into: "For any manual assignment detected or removed, a Slack notification should be sent with appropriate details."

**Consolidated Properties:**

After consolidation, we have:
- Original: 50 testable items (45 properties + 5 examples)
- After consolidation: 40 testable items (35 properties + 5 examples)

The consolidation removes redundancy while maintaining complete coverage of all requirements.



## Correctness Properties (Continued)

Based on the prework analysis and property reflection, here are the consolidated correctness properties:

### Core Matching and Sync Properties

Property 1: Configuration loading correctness
*For any* valid sync configuration, the system should correctly parse and load all mapping rules with their attribute conditions
**Validates: Requirements 1.1, 2.1, 2.2**

Property 2: User attribute retrieval completeness
*For any* sync operation, the system should retrieve all users from the Identity Store with their complete attribute sets
**Validates: Requirements 1.2**

Property 3: Attribute matching correctness
*For any* user with attributes and any set of mapping rules, the system should correctly identify all rules that the user matches based on exact string matching and AND logic
**Validates: Requirements 1.3, 2.3, 2.4**

Property 4: Membership addition idempotence
*For any* user who matches a rule but is not a group member, the system should add them to the group, and subsequent sync operations should not attempt to re-add them
**Validates: Requirements 1.4**

Property 5: Membership removal correctness
*For any* user who is a group member but does not match any rules, the system should remove them from the group
**Validates: Requirements 1.5**

Property 6: Managed group isolation
*For any* group not in the managed groups list, the system should perform no operations (no adds, removes, or monitoring)
**Validates: Requirements 10.4**

Property 7: Managed group processing completeness
*For any* group in the managed groups list, the system should evaluate all users against rules and monitor for manual assignments
**Validates: Requirements 10.2, 10.3**

### Configuration Validation Properties

Property 8: Invalid group reference handling
*For any* mapping rule that references a group ID not in the managed groups list, the system should log an error and skip that rule without failing other rules
**Validates: Requirements 2.5**

Property 9: Configuration validation completeness
*For any* configuration with missing required fields (managed groups or mapping rules), the system should produce validation errors and not proceed with sync
**Validates: Requirements 7.4, 7.5, 10.1, 10.5**

### Manual Assignment Detection Properties

Property 10: Manual assignment detection accuracy
*For any* user in a managed group, the system should correctly determine whether they were added by sync (matches rules) or manually (doesn't match rules)
**Validates: Requirements 3.1, 3.2**

Property 11: Manual assignment notification
*For any* manually-added user who doesn't match rules, the system should log a warning and send a Slack notification with user and group details
**Validates: Requirements 3.3, 3.4, 4.5**

Property 12: Policy-based removal behavior
*For any* manual assignment, when policy is "warn" the user should not be removed, and when policy is "remove" the user should be removed
**Validates: Requirements 3.5, 4.1, 4.2, 4.3**

### Audit Logging Properties

Property 13: Audit entry creation for all operations
*For any* sync operation (add, remove, or manual detection), an audit entry with the correct operation type ("sync_add", "sync_remove", or "manual_detected") should be written to S3
**Validates: Requirements 4.4, 6.1, 6.3, 6.4**

Property 14: User addition notification
*For any* user added to a group via sync, a Slack notification should be sent with user and group details
**Validates: Requirements 6.2**

Property 15: Audit entry completeness
*For any* audit entry, it should include all required fields: user ID, user email, group ID, matched attribute values, timestamp, and operation type
**Validates: Requirements 6.5**

Property 16: Audit entry storage consistency
*For any* audit entry, it should be written to the same S3 bucket and use the same partitioning scheme as existing SSO Elevator audit logs
**Validates: Requirements 6.6**

### Operational Properties

Property 17: Sync operation logging
*For any* sync operation, the system should log start time, end time, and summary statistics (users evaluated, groups processed, users added/removed, errors)
**Validates: Requirements 5.3, 5.4**

Property 18: Error resilience
*For any* error during sync (API failure, missing group, add failure, remove failure), the system should log the error, continue processing remaining items, and send a summary notification to Slack
**Validates: Requirements 5.5, 8.1, 8.2, 8.3, 8.4, 8.5**

### Caching Properties

Property 19: Cache utilization
*For any* cached resource (users or groups), if cache is valid and not expired, the system should use cached data instead of making API calls
**Validates: Requirements 9.1, 9.2**

Property 20: Cache fallback
*For any* cache miss or cache error, the system should fall back to direct API calls without failing the sync operation
**Validates: Requirements 9.3**

Property 21: Cache update on success
*For any* successful sync operation, the system should update the cache with fresh data for future operations
**Validates: Requirements 9.4**

Property 22: Cache TTL respect
*For any* cache configuration with TTL settings, the system should respect those settings when determining cache validity
**Validates: Requirements 9.5**

## Error Handling

### Error Categories

1. **Configuration Errors**
   - Missing required configuration (managed groups, mapping rules)
   - Invalid group IDs in mapping rules
   - Malformed attribute conditions
   - **Handling**: Log error, send Slack notification, abort sync operation

2. **API Errors**
   - Identity Store API unavailable
   - Rate limiting
   - Permission errors
   - **Handling**: Log error, send Slack notification, retry on next scheduled run

3. **Resource Not Found Errors**
   - Group in configuration doesn't exist
   - User referenced in membership doesn't exist
   - **Handling**: Log error, skip that resource, continue with others

4. **Operation Errors**
   - Failed to add user to group
   - Failed to remove user from group
   - Failed to write audit entry
   - **Handling**: Log error, continue with other operations, include in summary notification

### Error Recovery Strategy

```python
def perform_sync_with_error_handling():
    """Main sync with comprehensive error handling"""
    errors = []
    
    try:
        # Load configuration
        config = load_sync_config()
        validate_sync_config(config)  # Raises on validation errors
    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        send_slack_notification(f"Sync aborted due to configuration error: {e}")
        return
    
    try:
        # Get users and groups
        users = get_users_with_attributes()  # Uses cache with fallback
        groups = get_managed_groups(config.managed_groups)
    except APIError as e:
        logger.error(f"API error during data retrieval: {e}")
        send_slack_notification(f"Sync failed due to API error: {e}")
        return
    
    # Process each group independently
    for group in groups:
        try:
            sync_group_membership(group, users, config)
        except Exception as e:
            logger.error(f"Error syncing group {group.id}: {e}")
            errors.append(f"Group {group.name}: {e}")
            # Continue with next group
    
    # Send summary
    if errors:
        send_slack_notification(
            f"Sync completed with {len(errors)} errors:\n" + "\n".join(errors)
        )
    else:
        logger.info("Sync completed successfully")
```

## Testing Strategy

### Unit Testing

Unit tests will cover:

1. **Attribute Matching Logic**
   - Test `AttributeCondition.matches()` with various attribute values
   - Test `AttributeMappingRule.matches()` with multiple conditions
   - Test `AttributeMapper.get_target_groups_for_user()` with various user profiles

2. **Configuration Parsing**
   - Test `load_sync_config()` with valid and invalid configurations
   - Test `validate_sync_config()` with edge cases

3. **State Management**
   - Test `SyncStateManager.compute_sync_actions()` with various current/desired states
   - Test action prioritization and deduplication

4. **Audit Logging**
   - Test audit entry creation for all operation types
   - Test S3 path construction and partitioning

### Property-Based Testing

Property-based tests will use **Hypothesis** (Python's property testing library) to verify universal properties across many randomly generated inputs.

**Configuration**:
- Each property test should run a minimum of 100 iterations
- Tests should generate diverse inputs (users, attributes, rules, groups)
- Tests should use appropriate strategies for generating valid test data

**Key Property Tests**:

1. **Property Test 1: Configuration loading correctness**
   - **Feature: attribute-based-group-sync, Property 1: Configuration loading correctness**
   - Generate random valid configurations
   - Verify all rules are correctly parsed with their conditions

2. **Property Test 2: Attribute matching correctness**
   - **Feature: attribute-based-group-sync, Property 3: Attribute matching correctness**
   - Generate random users with attributes and random rules
   - Verify matching logic correctly identifies matches

3. **Property Test 3: Membership addition idempotence**
   - **Feature: attribute-based-group-sync, Property 4: Membership addition idempotence**
   - Generate random users and groups
   - Verify users are added once and not re-added on subsequent syncs

4. **Property Test 4: Managed group isolation**
   - **Feature: attribute-based-group-sync, Property 6: Managed group isolation**
   - Generate random managed and non-managed groups
   - Verify no operations occur on non-managed groups

5. **Property Test 5: Manual assignment detection accuracy**
   - **Feature: attribute-based-group-sync, Property 10: Manual assignment detection accuracy**
   - Generate random group memberships with and without matching rules
   - Verify correct classification of manual vs sync-added users

6. **Property Test 6: Policy-based removal behavior**
   - **Feature: attribute-based-group-sync, Property 12: Policy-based removal behavior**
   - Generate random manual assignments with different policies
   - Verify removal occurs only when policy is "remove"

7. **Property Test 7: Audit entry creation for all operations**
   - **Feature: attribute-based-group-sync, Property 13: Audit entry creation for all operations**
   - Generate random sync operations
   - Verify audit entries are created with correct operation types

8. **Property Test 8: User addition notification**
   - **Feature: attribute-based-group-sync, Property 14: User addition notification**
   - Generate random user additions
   - Verify Slack notifications are sent with correct details

9. **Property Test 9: Error resilience**
   - **Feature: attribute-based-group-sync, Property 18: Error resilience**
   - Generate random errors during sync
   - Verify processing continues and errors are logged

10. **Property Test 10: Cache utilization**
    - **Feature: attribute-based-group-sync, Property 19: Cache utilization**
    - Generate random cache states (hit/miss, expired/valid)
    - Verify correct cache usage decisions

11. **Property Test 11: Cache fallback**
    - **Feature: attribute-based-group-sync, Property 20: Cache fallback**
    - Generate random cache errors
    - Verify fallback to API calls

### Integration Testing

Integration tests will verify:

1. **Identity Store Integration**
   - Mock Identity Store API responses
   - Verify correct API calls for users, groups, and memberships
   - Test pagination handling

2. **S3 Integration**
   - Mock S3 API
   - Verify audit entries are written to correct paths
   - Test cache read/write operations

3. **Slack Integration**
   - Mock Slack API
   - Verify notifications are sent for manual assignments and errors
   - Test notification formatting

4. **EventBridge Integration**
   - Verify Lambda is triggered by schedule
   - Test event payload structure

### Test Data Strategies

For property-based testing, we'll use Hypothesis strategies:

```python
from hypothesis import strategies as st

# Strategy for generating user attributes
user_attributes_strategy = st.dictionaries(
    keys=st.sampled_from(["department", "employeeType", "costCenter", "jobTitle"]),
    values=st.text(min_size=1, max_size=50),
    min_size=1,
    max_size=4
)

# Strategy for generating users
user_strategy = st.builds(
    UserWithAttributes,
    user_id=st.uuids().map(str),
    username=st.text(min_size=3, max_size=20),
    email=st.emails(),
    attributes=user_attributes_strategy
)

# Strategy for generating attribute conditions
condition_strategy = st.builds(
    AttributeCondition,
    attribute_name=st.sampled_from(["department", "employeeType", "costCenter"]),
    expected_value=st.text(min_size=1, max_size=50)
)

# Strategy for generating mapping rules
rule_strategy = st.builds(
    AttributeMappingRule,
    group_id=st.uuids().map(str),
    conditions=st.lists(condition_strategy, min_size=1, max_size=3)
)
```

## Deployment Considerations

### Terraform Resources

New resources to be created:

1. **Lambda Function**: `attribute-syncer`
   - Runtime: Python 3.12
   - Memory: 512 MB (configurable)
   - Timeout: 5 minutes (configurable)
   - Environment variables: Same as existing lambdas + sync-specific config

2. **EventBridge Rule**: `sso-elevator-attribute-sync`
   - Schedule expression: Configurable (default: `rate(1 hour)`)
   - Target: attribute-syncer Lambda

3. **IAM Role**: `sso-elevator-attribute-syncer-role`
   - Permissions: Same as existing lambdas + Identity Store read/write

4. **CloudWatch Log Group**: `/aws/lambda/attribute-syncer`
   - Retention: Configurable (default: 365 days)

### Terraform Variables

```hcl
variable "attribute_sync_enabled" {
  description = "Enable attribute-based group sync feature"
  type        = bool
  default     = false
}

variable "attribute_sync_managed_groups" {
  description = "List of group names to manage via attribute sync"
  type        = list(string)
  default     = []
}

variable "attribute_sync_rules" {
  description = "Attribute mapping rules for group sync"
  type = list(object({
    group_name = string
    attributes = map(string)
  }))
  default = []
}

variable "attribute_sync_manual_assignment_policy" {
  description = "Policy for handling manual assignments: 'warn' or 'remove'"
  type        = string
  default     = "warn"
  
  validation {
    condition     = contains(["warn", "remove"], var.attribute_sync_manual_assignment_policy)
    error_message = "Policy must be either 'warn' or 'remove'"
  }
}

variable "attribute_sync_schedule" {
  description = "Schedule expression for attribute sync (e.g., 'rate(1 hour)' or 'cron(0 * * * ? *)')"
  type        = string
  default     = "rate(1 hour)"
}

variable "attribute_sync_lambda_memory" {
  description = "Memory allocation for attribute syncer Lambda (MB)"
  type        = number
  default     = 512
}

variable "attribute_sync_lambda_timeout" {
  description = "Timeout for attribute syncer Lambda (seconds)"
  type        = number
  default     = 300
}
```

### Validation Logic

```hcl
# Terraform validation
locals {
  attribute_sync_validation_errors = concat(
    var.attribute_sync_enabled && length(var.attribute_sync_managed_groups) == 0 ? 
      ["attribute_sync_managed_groups must not be empty when attribute_sync_enabled is true"] : [],
    
    var.attribute_sync_enabled && length(var.attribute_sync_rules) == 0 ? 
      ["attribute_sync_rules must not be empty when attribute_sync_enabled is true"] : [],
    
    # Validate all rules reference managed groups
    [for rule in var.attribute_sync_rules : 
      "Rule for group ${rule.group_name} references a group not in managed_groups list"
      if !contains(var.attribute_sync_managed_groups, rule.group_name)
    ]
  )
}

resource "null_resource" "attribute_sync_validation" {
  count = length(local.attribute_sync_validation_errors) > 0 ? 1 : 0
  
  provisioner "local-exec" {
    command = "echo 'Validation errors: ${join(", ", local.attribute_sync_validation_errors)}' && exit 1"
  }
}
```

### Migration Path

For existing SSO Elevator deployments:

1. **Phase 1: Deploy with feature disabled (default)**
   - Update Terraform module version
   - Apply changes (no new resources created)
   - Verify existing functionality unchanged

2. **Phase 2: Configure and enable**
   - Define managed groups list
   - Define attribute mapping rules
   - Set `attribute_sync_enabled = true`
   - Apply changes (creates new Lambda and schedule)

3. **Phase 3: Monitor and adjust**
   - Review Slack notifications for manual assignments
   - Adjust mapping rules as needed
   - Consider changing policy from "warn" to "remove"

### Rollback Strategy

If issues arise:

1. **Immediate**: Set `attribute_sync_enabled = false` and apply
   - Stops scheduled syncs
   - Preserves existing group memberships
   - No data loss

2. **Complete**: Remove all attribute sync configuration
   - Delete Lambda function
   - Delete EventBridge rule
   - Existing audit logs remain in S3

## Security Considerations

1. **IAM Permissions**
   - Lambda needs `identitystore:ListUsers`, `identitystore:DescribeUser`
   - Lambda needs `identitystore:ListGroupMemberships`, `identitystore:CreateGroupMembership`, `identitystore:DeleteGroupMembership`
   - Lambda needs `identitystore:DescribeGroup`
   - Follow principle of least privilege

2. **Audit Trail**
   - All sync operations logged to S3 with immutability (object lock)
   - Includes user ID, group ID, matched attributes, timestamp
   - Enables compliance and forensics

3. **Manual Assignment Detection**
   - Prevents unauthorized access via manual group additions
   - Configurable enforcement (warn vs remove)
   - Slack notifications for visibility

4. **Configuration Security**
   - Attribute mapping rules stored in Terraform state (encrypted at rest)
   - No sensitive data in attribute values (use attribute names only)
   - Managed groups list explicitly defined (no wildcards)

## Performance Considerations

1. **API Call Optimization**
   - Use caching for user and group data (TTL: 1 hour default)
   - Batch operations where possible
   - Implement exponential backoff for rate limiting

2. **Lambda Sizing**
   - Default: 512 MB memory, 5 minute timeout
   - Adjust based on number of users and groups
   - Monitor CloudWatch metrics for optimization

3. **Sync Frequency**
   - Default: 1 hour
   - Adjust based on attribute change frequency
   - Consider cost vs freshness tradeoff

4. **Scalability**
   - Tested with up to 10,000 users and 100 groups
   - For larger deployments, consider:
     - Increasing Lambda memory and timeout
     - Implementing pagination for large user sets
     - Running sync less frequently

## Monitoring and Observability

### Slack Notifications

Notifications sent for:

1. **Users added to groups** (always) - Notifies when users are automatically added based on attribute matching
2. **Manual assignments detected** (always) - Warns about users who don't match rules
3. **Manual assignments removed** (when policy = "remove") - Confirms removal of non-matching users
4. **Sync errors** (always) - Reports any errors encountered during sync

### Log Structure

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "service": "attribute-syncer",
  "operation": "sync",
  "user_id": "12345678-1234-1234-1234-123456789012",
  "user_email": "john.doe@example.com",
  "group_id": "87654321-4321-4321-4321-210987654321",
  "group_name": "Engineering",
  "action": "add",
  "matched_attributes": {
    "department": "Engineering",
    "employeeType": "FullTime"
  },
  "duration_ms": 1234
}
```

## Future Enhancements

Potential future improvements (not in scope for initial implementation):

1. **Advanced Matching**
   - Support for regex patterns in attribute values
   - Support for OR logic between conditions
   - Support for NOT conditions (exclusions)

2. **Dry Run Mode**
   - Preview changes without applying them
   - Generate report of what would change

3. **Incremental Sync**
   - Track last sync time
   - Only process users with attribute changes since last sync
   - Requires additional state storage

4. **Multi-Attribute Mapping**
   - Map different attribute combinations to same group
   - Support for priority/precedence rules

5. **Approval Workflow**
   - Require approval for certain attribute-based assignments
   - Integration with existing SSO Elevator approval flow

6. **Attribute Change Notifications**
   - Notify when user attributes change
   - Alert on suspicious attribute modifications

7. **Self-Service Attribute Updates**
   - Allow users to request attribute changes via Slack
   - Integrate with HR systems for validation
