# Requirements Document

## Introduction

This document outlines the requirements for migrating Lambda configuration from environment variables to S3 object storage. Currently, the SSO Elevator system stores approval configuration (statements and group_statements) as JSON-encoded environment variables in Lambda functions. Users have reported that their configurations exceed AWS Lambda's environment variable size limits (4KB total). This feature will move the approval configuration to an S3 object while keeping other environment variables intact.

## Glossary

- **Lambda_Function**: The AWS Lambda functions (access-requester and access-revoker) that process SSO elevation requests
- **Config_Bucket**: The existing S3 bucket (sso-elevator-config) used for storing configuration and cache data
- **Approval_Config**: The statements and group_statements that define who can approve access requests and what resources they can approve
- **Terraform_Module**: The infrastructure-as-code module that provisions AWS resources for SSO Elevator
- **Environment_Variables**: Key-value pairs passed to Lambda functions at runtime, limited to 4KB total size
- **S3_Object**: A file stored in an S3 bucket that can be retrieved by Lambda functions

## Requirements

### Requirement 1

**User Story:** As a platform administrator, I want to store approval configuration in S3 instead of environment variables, so that I can define large configurations without hitting Lambda limits

#### Acceptance Criteria

1. WHEN THE Terraform_Module is applied, THE Terraform_Module SHALL create an S3_Object in the Config_Bucket containing the Approval_Config
2. THE S3_Object SHALL be stored at the path "config/approval-config.json" within the Config_Bucket
3. THE S3_Object SHALL contain a JSON document with "statements" and "group_statements" keys
4. THE Terraform_Module SHALL remove the STATEMENTS and GROUP_STATEMENTS keys from the Lambda_Function Environment_Variables
5. THE Terraform_Module SHALL add a CONFIG_S3_KEY Environment_Variable to the Lambda_Function specifying the S3_Object path

### Requirement 2

**User Story:** As a Lambda function, I want to load approval configuration from S3 on startup, so that I can access the configuration without environment variable limits

#### Acceptance Criteria

1. WHEN THE Lambda_Function starts, THE Lambda_Function SHALL retrieve the S3_Object from the Config_Bucket using the CONFIG_S3_KEY Environment_Variable
2. IF the S3_Object retrieval fails, THEN THE Lambda_Function SHALL log an error and raise an exception
3. WHEN THE Lambda_Function parses the S3_Object content, THE Lambda_Function SHALL validate the JSON structure contains "statements" and "group_statements" keys
4. THE Lambda_Function SHALL parse the statements and group_statements using the existing parse_statement and parse_group_statement functions
5. THE Lambda_Function SHALL populate the Config object with the parsed Approval_Config

### Requirement 3

**User Story:** As a platform administrator, I want the Lambda functions to have read access to the configuration S3 object, so that they can retrieve the approval configuration

#### Acceptance Criteria

1. THE Terraform_Module SHALL grant the Lambda_Function IAM role permission to perform s3:GetObject on the S3_Object
2. THE Terraform_Module SHALL grant the Lambda_Function IAM role permission to perform s3:ListBucket on the Config_Bucket
3. IF the Config_Bucket uses KMS encryption, THEN THE Terraform_Module SHALL grant the Lambda_Function IAM role permission to use the KMS key

### Requirement 4

**User Story:** As a platform administrator, I want non-approval configuration to remain in environment variables, so that other settings continue to work as before

#### Acceptance Criteria

1. THE Terraform_Module SHALL retain all existing Environment_Variables except STATEMENTS and GROUP_STATEMENTS
2. THE Lambda_Function SHALL continue to read non-approval configuration from Environment_Variables
3. THE Lambda_Function SHALL support the existing Config class interface for accessing all configuration values

### Requirement 5

**User Story:** As a developer, I want the configuration loading to be testable, so that I can verify the S3 integration works correctly

#### Acceptance Criteria

1. THE Lambda_Function SHALL provide a mechanism to mock S3 client calls during testing
2. THE Lambda_Function SHALL handle S3 access errors gracefully with descriptive error messages
3. THE Lambda_Function SHALL log the successful loading of Approval_Config from S3 at INFO level
