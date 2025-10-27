# Design Document

## Overview

This design addresses the Lambda environment variable size limitation by migrating approval configuration (statements and group_statements) from environment variables to S3 storage. The solution maintains backward compatibility for all other configuration parameters while enabling support for large approval configurations that exceed the 4KB Lambda environment variable limit.

The design leverages the existing config S3 bucket and modifies the configuration loading mechanism in the Lambda functions to fetch approval configuration from S3 during initialization.

## Architecture

### Current State

- Approval configuration stored as JSON-encoded environment variables: `STATEMENTS` and `GROUP_STATEMENTS`
- Lambda functions read configuration via `pydantic_settings.BaseSettings` which automatically loads from environment variables
- Config bucket already exists for caching accounts and permission sets

### Target State

- Approval configuration stored as JSON file in S3 at `config/approval-config.json`
- Lambda functions fetch configuration from S3 during initialization
- All other environment variables remain unchanged
- Terraform creates and manages the S3 object containing approval configuration

## Components and Interfaces

### 1. Terraform Configuration Changes

#### S3 Object Resource

Create a new `aws_s3_object` resource in Terraform that:
- Stores approval configuration at `config/approval-config.json` in the config bucket
- Contains JSON with structure: `{"statements": [...], "group_statements": [...]}`
- Uses the existing `var.config` and `var.group_config` variables as source data
- Applies appropriate content type and encryption settings

#### Lambda Environment Variable Updates

Modify Lambda environment variables in `slack_handler_lambda.tf` and `perm_revoker_lambda.tf`:
- Remove: `STATEMENTS` and `GROUP_STATEMENTS` environment variables
- Add: `CONFIG_S3_KEY = "config/approval-config.json"` environment variable
- Keep: All other existing environment variables unchanged

#### IAM Permission Updates

The Lambda functions already have S3 permissions for the config bucket (added for caching feature), so no additional IAM changes are required. The existing permissions include:
- `s3:GetObject` on config bucket objects
- `s3:PutObject` on config bucket objects
- `s3:ListBucket` on config bucket

### 2. Python Configuration Module Changes

#### New Configuration Loading Function

Add a new function `load_approval_config_from_s3()` in `src/config.py`:


```python
def load_approval_config_from_s3(s3_client, bucket_name: str, s3_key: str) -> dict:
    """
    Load approval configuration from S3.
    
    Returns dict with 'statements' and 'group_statements' keys.
    Raises exception if S3 retrieval or JSON parsing fails.
    """
```

This function will:
- Retrieve the S3 object using boto3 S3 client
- Parse the JSON content
- Validate that required keys exist
- Return the parsed configuration dictionary
- Log errors and raise exceptions on failure

#### Modified Config Class

Update the `Config` class in `src/config.py`:
- Remove `statements` and `group_statements` from direct environment variable loading
- Add new environment variable: `config_s3_key: str`
- Modify the `@model_validator` to call `load_approval_config_from_s3()` when initializing
- Parse statements and group_statements from S3 data instead of environment variables

#### Configuration Initialization Flow

```
1. Lambda starts
2. Config class instantiation begins
3. BaseSettings loads environment variables (excluding statements/group_statements)
4. model_validator runs:
   a. Check if config_s3_key is provided
   b. Create S3 client
   c. Call load_approval_config_from_s3()
   d. Parse statements using existing parse_statement()
   e. Parse group_statements using existing parse_group_statement()
   f. Populate accounts, permission_sets, groups from parsed data
5. Config object ready for use
```

## Data Models

### S3 Configuration Object Structure

```json
{
  "statements": [
    {
      "PermissionSet": "string or array",
      "Resource": "string or array",
      "Approvers": "string or array (optional)",
      "ResourceType": "string (optional)",
      "ApprovalIsNotRequired": "boolean (optional)",
      "AllowSelfApproval": "boolean (optional)"
    }
  ],
  "group_statements": [
    {
      "Resource": "string or array",
      "Approvers": "string or array (optional)",
      "ApprovalIsNotRequired": "boolean (optional)",
      "AllowSelfApproval": "boolean (optional)"
    }
  ]
}
```

This structure matches the existing JSON format currently used in environment variables.

### Config Class Updates

The `Config` class will have these changes:
- New field: `config_s3_key: str` (from environment variable)
- Existing fields remain: `statements: frozenset[Statement]`, `group_statements: frozenset[GroupStatement]`
- Loading mechanism changes but interface remains the same

## Error Handling

### S3 Retrieval Errors

When S3 object retrieval fails:
1. Log error with full context (bucket, key, exception details)
2. Raise exception to prevent Lambda from starting with invalid configuration
3. Lambda will fail initialization and AWS will retry or report error

### JSON Parsing Errors

When JSON parsing fails:
1. Log error with S3 content preview
2. Raise exception with descriptive message
3. Lambda initialization fails

### Missing Configuration Keys

When required keys are missing from S3 object:
1. Log warning about missing keys
2. Default to empty sets for missing statements/group_statements
3. Continue initialization (matches current behavior when env vars are empty)



## Testing Strategy

### Unit Tests

1. Test `load_approval_config_from_s3()` function:
   - Mock S3 client responses
   - Test successful retrieval and parsing
   - Test S3 access errors (NoSuchKey, AccessDenied)
   - Test JSON parsing errors
   - Test missing keys in JSON

2. Test `Config` class initialization:
   - Mock S3 client in model_validator
   - Test with valid S3 configuration
   - Test with empty statements/group_statements
   - Test fallback to environment variables when S3 key not provided

3. Test statement parsing:
   - Verify existing parse_statement() and parse_group_statement() work with S3-loaded data
   - Test various statement configurations

### Integration Tests

1. Test Lambda initialization:
   - Deploy with S3 configuration
   - Verify Lambda starts successfully
   - Verify configuration is loaded correctly
   - Check logs for successful S3 retrieval

2. Test Terraform deployment:
   - Verify S3 object is created with correct content
   - Verify Lambda environment variables are updated
   - Verify IAM permissions are sufficient

### Manual Testing

1. Test with small configuration (< 4KB)
2. Test with large configuration (> 4KB) that previously failed
3. Test approval workflows with S3-loaded configuration
4. Verify audit logs still work correctly

## Implementation Notes

### S3 Client Initialization

The S3 client should be created within the configuration loading logic to avoid circular dependencies. Since `main.py` and `revoker.py` already create S3 clients, we can follow the same pattern.

### Caching Considerations

The configuration is loaded once during Lambda initialization (cold start). Lambda containers are reused across invocations, so the configuration remains in memory. No additional caching mechanism is needed.

### Terraform State Management

The S3 object content is derived from Terraform variables, so any configuration changes require Terraform apply. This maintains the existing deployment workflow.

### Migration Path

1. Deploy Terraform changes (creates S3 object, updates Lambda env vars)
2. Lambda functions automatically use new configuration source on next invocation
3. No manual migration steps required
4. Old environment variables can be removed after successful deployment

## Security Considerations

1. S3 bucket encryption: Config bucket already supports KMS encryption via `var.config_bucket_kms_key_arn`
2. IAM permissions: Lambda already has necessary S3 permissions
3. Configuration validation: Existing pydantic validation continues to work
4. Audit trail: S3 object versioning is enabled on config bucket for change tracking
