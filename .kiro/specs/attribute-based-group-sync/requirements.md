# Requirements Document

## Introduction

This feature adds automatic user-to-group synchronization based on IAM Identity Center user attributes. When enabled, SSO Elevator will automatically add users to groups based on attribute matching rules, and will monitor groups to detect and handle manually-added users that don't match the attribute criteria.

This feature is designed to work alongside the existing temporary elevated access functionality, providing a complementary automated provisioning capability for attribute-based access control (ABAC).

## Glossary

- **IAM Identity Center**: AWS service for managing SSO access (formerly AWS SSO)
- **Identity Store**: The user directory within IAM Identity Center containing users and their attributes
- **User Attributes**: Properties associated with users in the Identity Store (e.g., department, costCenter, jobTitle)
- **Group**: A collection of users in IAM Identity Center
- **Attribute Mapping Rule**: A configuration that defines which users should be added to which groups based on attribute values
- **SSO Elevator**: The existing system for temporary elevated access management
- **Sync Operation**: The process of adding/removing users from groups based on attribute rules
- **Manual Assignment**: A user added to a group outside of the attribute sync system

## Requirements

### Requirement 1

**User Story:** As a security administrator, I want to automatically provision users to groups based on their attributes, so that access management scales with organizational structure without manual intervention.

#### Acceptance Criteria

1. WHEN the attribute sync feature is enabled THEN the system SHALL read user attribute mapping rules from configuration
2. WHEN a sync operation runs THEN the system SHALL query all users from the Identity Store with their attributes
3. WHEN evaluating a user against mapping rules THEN the system SHALL match user attributes against all configured rules
4. WHEN a user matches a rule THEN the system SHALL add the user to the corresponding group if not already a member
5. WHEN a user no longer matches a rule THEN the system SHALL remove the user from the corresponding group

### Requirement 2

**User Story:** As a security administrator, I want to define attribute-based mapping rules in configuration, so that I can control which users get assigned to which groups based on their organizational attributes.

#### Acceptance Criteria

1. WHEN defining a mapping rule THEN the system SHALL accept a group ID as the target resource from the managed groups list
2. WHEN defining a mapping rule THEN the system SHALL accept one or more attribute conditions (attribute name and expected value)
3. WHEN defining a mapping rule THEN the system SHALL support exact string matching for attribute values
4. WHEN defining a mapping rule THEN the system SHALL support multiple attribute conditions with AND logic
5. WHEN a mapping rule references a group not in the managed list THEN the system SHALL log an error and skip that rule

### Requirement 3

**User Story:** As a security administrator, I want the system to detect manually-added users in managed groups, so that I can maintain control over group membership and prevent unauthorized access.

#### Acceptance Criteria

1. WHEN a sync operation runs THEN the system SHALL identify all users currently in managed groups
2. WHEN a user is in a managed group THEN the system SHALL verify if the user was added by the sync system or manually
3. WHEN a user was manually added and does not match attribute rules THEN the system SHALL log a warning with user and group details
4. WHEN a user was manually added and does not match attribute rules THEN the system SHALL send a notification to the Slack channel
5. WHEN the removal policy is set to automatic THEN the system SHALL remove manually-added users who don't match rules

### Requirement 4

**User Story:** As a security administrator, I want to control whether manually-added users are warned about or automatically removed, so that I can choose the appropriate enforcement level for my organization.

#### Acceptance Criteria

1. WHEN configuring the feature THEN the system SHALL accept a policy setting for handling manual assignments
2. WHEN the policy is set to "warn" THEN the system SHALL only log and notify about manual assignments without removing them
3. WHEN the policy is set to "remove" THEN the system SHALL automatically remove manual assignments that don't match rules
4. WHEN removing a manual assignment THEN the system SHALL log the removal operation to the audit bucket
5. WHEN removing a manual assignment THEN the system SHALL send a notification to Slack with user and group details

### Requirement 5

**User Story:** As a security administrator, I want the sync operation to run on a configurable schedule, so that group memberships stay current with user attribute changes.

#### Acceptance Criteria

1. WHEN deploying the feature THEN the system SHALL accept a schedule expression for sync frequency
2. WHEN the schedule triggers THEN the system SHALL invoke the sync Lambda function
3. WHEN a sync operation starts THEN the system SHALL log the start time and operation type
4. WHEN a sync operation completes THEN the system SHALL log the completion time and summary statistics
5. WHEN a sync operation fails THEN the system SHALL log the error and send a notification to Slack

### Requirement 6

**User Story:** As a security administrator, I want all sync operations logged to the audit bucket and notified via Slack, so that I have a complete record of automated provisioning actions and visibility into access changes.

#### Acceptance Criteria

1. WHEN a user is added to a group via sync THEN the system SHALL write an audit entry with operation type "sync_add"
2. WHEN a user is added to a group via sync THEN the system SHALL send a Slack notification with user and group details
3. WHEN a user is removed from a group via sync THEN the system SHALL write an audit entry with operation type "sync_remove"
4. WHEN a manual assignment is detected THEN the system SHALL write an audit entry with operation type "manual_detected"
5. WHEN writing audit entries THEN the system SHALL include user ID, user email, group ID, attribute values, and timestamp
6. WHEN writing audit entries THEN the system SHALL use the same S3 bucket and partitioning scheme as existing audit logs

### Requirement 7

**User Story:** As a security administrator, I want the attribute sync feature to be optional and disabled by default, so that existing deployments are not affected and I can enable it when ready.

#### Acceptance Criteria

1. WHEN deploying the module THEN the attribute sync feature SHALL be disabled by default
2. WHEN the feature is disabled THEN the system SHALL NOT create the sync Lambda function
3. WHEN the feature is disabled THEN the system SHALL NOT create the sync schedule
4. WHEN enabling the feature THEN the system SHALL require explicit configuration of attribute mapping rules
5. WHEN enabling the feature THEN the system SHALL validate that at least one mapping rule is provided

### Requirement 8

**User Story:** As a security administrator, I want the system to handle errors gracefully during sync operations, so that temporary failures don't disrupt the entire access management system.

#### Acceptance Criteria

1. WHEN the Identity Store API is unavailable THEN the system SHALL log the error and retry on the next scheduled run
2. WHEN a group in the configuration does not exist THEN the system SHALL log an error and continue processing other groups
3. WHEN adding a user to a group fails THEN the system SHALL log the error and continue processing other users
4. WHEN removing a user from a group fails THEN the system SHALL log the error and continue processing other users
5. WHEN the sync operation encounters errors THEN the system SHALL send a summary notification to Slack with error count

### Requirement 9

**User Story:** As a developer, I want the attribute sync system to integrate with the existing SSO Elevator caching mechanism, so that API calls are minimized and performance is optimized.

#### Acceptance Criteria

1. WHEN retrieving user lists THEN the system SHALL use cached data if available and not expired
2. WHEN retrieving group information THEN the system SHALL use cached data if available and not expired
3. WHEN cache is unavailable THEN the system SHALL fall back to direct API calls
4. WHEN sync completes successfully THEN the system SHALL update the cache with fresh data
5. WHEN cache is enabled in configuration THEN the system SHALL respect the cache TTL settings

### Requirement 10

**User Story:** As a security administrator, I want to explicitly specify which groups are managed by attribute-based sync, so that only designated groups are monitored and all other groups remain under manual control.

#### Acceptance Criteria

1. WHEN configuring the feature THEN the system SHALL require an explicit list of managed group IDs
2. WHEN a group is in the managed list THEN the system SHALL add or remove users from that group based on attribute rules
3. WHEN a group is in the managed list THEN the system SHALL monitor that group for manual assignments
4. WHEN a group is NOT in the managed list THEN the system SHALL completely ignore that group
5. WHEN no managed groups are specified THEN the system SHALL log an error and not perform any sync operations
