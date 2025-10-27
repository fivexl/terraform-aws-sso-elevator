# Implementation Plan

- [x] 1. Create S3 object resource in Terraform for approval configuration
  - Create `aws_s3_object` resource in a new or existing Terraform file
  - Set bucket to the config bucket name
  - Set key to "config/approval-config.json"
  - Set content to JSON-encoded object with "statements" and "group_statements" keys from var.config and var.group_config
  - Set content_type to "application/json"
  - Set server_side_encryption to match config bucket encryption settings
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Update Lambda environment variables in Terraform
  - [x] 2.1 Remove STATEMENTS and GROUP_STATEMENTS from slack_handler_lambda.tf environment_variables
    - Remove the STATEMENTS line
    - Remove the GROUP_STATEMENTS line
    - _Requirements: 1.4_
  
  - [x] 2.2 Remove STATEMENTS and GROUP_STATEMENTS from perm_revoker_lambda.tf environment_variables
    - Remove the STATEMENTS line
    - Remove the GROUP_STATEMENTS line
    - _Requirements: 1.4_
  
  - [x] 2.3 Add CONFIG_S3_KEY environment variable to both Lambda functions
    - Add CONFIG_S3_KEY = "config/approval-config.json" to slack_handler_lambda.tf
    - Add CONFIG_S3_KEY = "config/approval-config.json" to perm_revoker_lambda.tf
    - _Requirements: 1.5_

- [x] 3. Implement S3 configuration loading in Python
  - [x] 3.1 Add load_approval_config_from_s3 function to src/config.py
    - Create function that accepts s3_client, bucket_name, and s3_key parameters
    - Implement S3 GetObject call to retrieve configuration
    - Parse JSON content from S3 response
    - Validate that "statements" and "group_statements" keys exist
    - Return parsed dictionary
    - Add error handling for S3 access errors with descriptive logging
    - Add error handling for JSON parsing errors with descriptive logging
    - _Requirements: 2.1, 2.2, 2.3, 5.2, 5.3_
  
  - [x] 3.2 Update Config class to load from S3
    - Add config_s3_key field to Config class as a string field
    - Modify get_accounts_and_permission_sets model_validator to create S3 client
    - Call load_approval_config_from_s3 with config bucket name and config_s3_key
    - Update validator to use S3-loaded data instead of environment variables for statements/group_statements
    - Keep existing parsing logic using parse_statement and parse_group_statement functions
    - Ensure accounts, permission_sets, and groups are still derived from parsed statements
    - _Requirements: 2.4, 2.5, 4.2, 4.3_

- [x] 4. Update tests to mock S3 configuration loading
  - [x] 4.1 Update test fixtures in src/tests/conftest.py
    - Add S3 mock fixture that returns approval configuration
    - Update existing config fixtures to use mocked S3 client
    - _Requirements: 5.1_
  
  - [x] 4.2 Add unit tests for load_approval_config_from_s3 function
    - Test successful S3 retrieval and JSON parsing
    - Test S3 NoSuchKey error handling
    - Test S3 AccessDenied error handling
    - Test invalid JSON error handling
    - Test missing keys in JSON structure
    - _Requirements: 5.1, 5.2_
  
  - [x] 4.3 Update existing config tests
    - Ensure Config class tests work with S3-loaded configuration
    - Verify statement parsing still works correctly
    - Verify group_statement parsing still works correctly
    - _Requirements: 5.1_

- [-] 5. Verify and test the complete implementation
  - Run all tests with `bash run-tests.sh` to ensure nothing is broken
  - Run pre-commit checks with `git add . && pre-commit run -a`
  - Verify Terraform plan shows expected changes (S3 object creation, env var updates)
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 4.1, 4.2, 4.3_
