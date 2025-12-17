# Implementation Plan

- [x] 1. Extend S3 audit logging for attribute sync operations
  - Add new audit entry types: "sync_add", "sync_remove", "manual_detected"
  - Add new fields: `sync_operation`, `matched_attributes`, `sso_user_email`
  - Ensure backward compatibility with existing audit entries
  - _Requirements: 6.1, 6.3, 6.4, 6.5, 6.6_

- [x] 1.1 Write property test for audit entry creation
  - **Property 13: Audit entry creation for all operations**
  - **Validates: Requirements 4.4, 6.1, 6.3, 6.4**

- [x] 1.2 Write property test for audit entry completeness
  - **Property 15: Audit entry completeness**
  - **Validates: Requirements 6.5**

- [x] 1.3 Write property test for audit entry storage consistency
  - **Property 16: Audit entry storage consistency**
  - **Validates: Requirements 6.6**

- [ ] 2. Create attribute mapping engine
  - Implement `AttributeCondition` class with exact string matching
  - Implement `AttributeMappingRule` class with AND logic for multiple conditions
  - Implement `AttributeMapper` class to evaluate users against rules
  - _Requirements: 1.3, 2.2, 2.3, 2.4_

- [ ] 2.1 Write property test for attribute matching correctness
  - **Property 3: Attribute matching correctness**
  - **Validates: Requirements 1.3, 2.3, 2.4**

- [ ] 3. Create sync configuration loader
  - Implement `SyncConfiguration` dataclass
  - Implement `load_sync_config()` to read from environment variables
  - Implement `validate_sync_config()` with validation rules
  - Handle group name to ID resolution
  - _Requirements: 1.1, 2.1, 4.1, 7.4, 7.5, 10.1_

- [ ] 3.1 Write property test for configuration loading correctness
  - **Property 1: Configuration loading correctness**
  - **Validates: Requirements 1.1, 2.1, 2.2**

- [ ] 3.2 Write property test for configuration validation
  - **Property 9: Configuration validation completeness**
  - **Validates: Requirements 7.4, 7.5, 10.1, 10.5**

- [ ] 4. Implement group name resolution
  - Create `resolve_group_names()` function to query Identity Store
  - Implement caching for name-to-ID mappings
  - Handle missing groups gracefully
  - _Requirements: 2.5, 8.2_

- [ ] 4.1 Write property test for invalid group reference handling
  - **Property 8: Invalid group reference handling**
  - **Validates: Requirements 2.5**

- [ ] 5. Create sync state manager
  - Implement `GroupMembershipState` dataclass
  - Implement `SyncAction` dataclass
  - Implement `SyncStateManager` class to compute required actions
  - Handle both add and remove operations
  - _Requirements: 1.4, 1.5, 3.1, 3.2_

- [ ] 5.1 Write property test for membership addition idempotence
  - **Property 4: Membership addition idempotence**
  - **Validates: Requirements 1.4**

- [ ] 5.2 Write property test for membership removal correctness
  - **Property 5: Membership removal correctness**
  - **Validates: Requirements 1.5**

- [ ] 6. Implement user and group data retrieval
  - Create `get_users_with_attributes()` function
  - Create `get_managed_groups()` function
  - Integrate with existing caching mechanism
  - Handle pagination for large user sets
  - _Requirements: 1.2, 9.1, 9.2, 9.3_

- [ ] 6.1 Write property test for cache utilization
  - **Property 19: Cache utilization**
  - **Validates: Requirements 9.1, 9.2**

- [ ] 6.2 Write property test for cache fallback
  - **Property 20: Cache fallback**
  - **Validates: Requirements 9.3**

- [ ] 7. Implement manual assignment detection
  - Create logic to identify users in groups who don't match rules
  - Distinguish between sync-added and manually-added users
  - _Requirements: 3.2, 3.3_

- [ ] 7.1 Write property test for manual assignment detection accuracy
  - **Property 10: Manual assignment detection accuracy**
  - **Validates: Requirements 3.1, 3.2**

- [ ] 8. Implement policy-based enforcement
  - Create logic to handle "warn" vs "remove" policies
  - Ensure removal only occurs when policy is "remove"
  - _Requirements: 3.5, 4.1, 4.2, 4.3_

- [ ] 8.1 Write property test for policy-based removal behavior
  - **Property 12: Policy-based removal behavior**
  - **Validates: Requirements 3.5, 4.1, 4.2, 4.3**

- [ ] 9. Implement Slack notifications
  - Create notification for users added to groups
  - Create notification for manual assignments detected
  - Create notification for manual assignments removed
  - Create notification for sync errors
  - Reuse existing Slack client and helpers
  - _Requirements: 3.4, 4.5, 5.5, 6.2_

- [ ] 9.1 Write property test for user addition notification
  - **Property 14: User addition notification**
  - **Validates: Requirements 6.2**

- [ ] 9.2 Write property test for manual assignment notification
  - **Property 11: Manual assignment notification**
  - **Validates: Requirements 3.3, 3.4, 4.5**

- [ ] 10. Create main attribute syncer Lambda function
  - Implement `lambda_handler()` entry point
  - Implement `perform_sync()` main orchestration logic
  - Implement error handling with graceful degradation
  - Implement sync operation logging
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 5.3, 5.4, 5.5, 8.1, 8.2, 8.3, 8.4, 8.5_

- [ ] 10.1 Write property test for sync operation logging
  - **Property 17: Sync operation logging**
  - **Validates: Requirements 5.3, 5.4**

- [ ] 10.2 Write property test for error resilience
  - **Property 18: Error resilience**
  - **Validates: Requirements 5.5, 8.1, 8.2, 8.3, 8.4, 8.5**

- [ ] 11. Implement managed group isolation
  - Ensure only managed groups are processed
  - Ensure non-managed groups are completely ignored
  - _Requirements: 10.2, 10.3, 10.4_

- [ ] 11.1 Write property test for managed group isolation
  - **Property 6: Managed group isolation**
  - **Validates: Requirements 10.4**

- [ ] 11.2 Write property test for managed group processing completeness
  - **Property 7: Managed group processing completeness**
  - **Validates: Requirements 10.2, 10.3**

- [ ] 12. Implement cache updates
  - Update cache after successful sync operations
  - Respect cache TTL settings
  - _Requirements: 9.4, 9.5_

- [ ] 12.1 Write property test for cache update on success
  - **Property 21: Cache update on success**
  - **Validates: Requirements 9.4**

- [ ] 12.2 Write property test for cache TTL respect
  - **Property 22: Cache TTL respect**
  - **Validates: Requirements 9.5**

- [ ] 13. Create Terraform resources for attribute syncer
  - Create Lambda function resource with Python 3.12 runtime
  - Create IAM role with Identity Store permissions
  - Create EventBridge schedule rule
  - Create CloudWatch log group
  - Add conditional creation based on `attribute_sync_enabled` variable
  - _Requirements: 5.1, 5.2, 7.1, 7.2, 7.3_

- [ ] 14. Add Terraform variables and validation
  - Add `attribute_sync_enabled` variable (default: false)
  - Add `attribute_sync_managed_groups` variable
  - Add `attribute_sync_rules` variable
  - Add `attribute_sync_manual_assignment_policy` variable with validation
  - Add `attribute_sync_schedule` variable
  - Add `attribute_sync_lambda_memory` and `attribute_sync_lambda_timeout` variables
  - Implement Terraform validation logic
  - _Requirements: 7.4, 7.5, 10.1, 10.5_

- [ ] 15. Update module documentation
  - Update README.md with attribute sync feature description
  - Add configuration examples
  - Add migration guide for existing deployments
  - Document rollback strategy
  - _Requirements: 7.1_

- [ ] 16. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
