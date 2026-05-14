# Requirements Document

## Introduction

Account access requests in Slack and Microsoft Teams look too similar. Requesters can accidentally pick the AWS **management account**, and approvers may approve without noticing. This feature makes management-account requests visually distinct and adds an explicit warning, using only AWS Organizations data (no name-based heuristics).

## Glossary

- **Management account**: The AWS account whose ID equals `Organization.MasterAccountId` returned by `organizations:DescribeOrganization`. User-facing copy uses *management account* (not “master account”), while code may read boto3 field `MasterAccountId`.
- **Member account**: Any other account in the organization.
- **Requester**: User submitting the access request (Slack or Teams).
- **Approver**: User who approves or discards the request in the approval channel.

## Requirements

### Requirement 1

**User Story:** As an operator, I want a single reliable rule for detecting the management account so labels and warnings are never based on fragile naming patterns.

#### Acceptance Criteria

1. WHEN the system decides whether an account is the management account THEN it SHALL compare `requested_account_id` to `MasterAccountId` from a successful `organizations:DescribeOrganization` response.
2. THE system SHALL NOT infer management account from account name, tags, suffixes, or patterns such as `management`, `root`, `*-org`.
3. IF `DescribeOrganization` fails or is unavailable THEN the system SHALL NOT label any account as management by guesswork; it SHALL log the failure with `logger.exception(...)`; UI MAY omit the management suffix and the approval warning (fail-safe, no false positives).

### Requirement 2

**User Story:** As a requester, I want the account dropdown to show which choice is the management account so I am less likely to select it by mistake.

#### Acceptance Criteria

1. WHEN the Slack modal builds the account `StaticSelect` THEN the option for the account whose ID equals the management account ID SHALL include an explicit suffix such as `(management account)` in the visible option text.
2. WHEN the Teams Task Module builds the account `Input.ChoiceSet` THEN the choice title for that account SHALL include the same explicit suffix.
3. WHEN the account is not the management account THEN option text SHALL remain clear and SHALL NOT include that suffix.

### Requirement 3

**User Story:** As an approver in Slack, I want separate account name and account ID lines and a prominent warning for management-account requests before Approve/Discard.

#### Acceptance Criteria

1. WHEN building the Slack approval message for **account** access (not group) THEN the main fields SHALL include separate lines for requester, account name, account ID, role name, reason, and duration.
2. WHEN the requested account is the management account THEN the message SHALL include a prominent warning block placed immediately **before** the `ActionsBlock` with Approve/Discard (e.g. markdown with emoji and bold, instructing careful review).
3. WHEN the request is **group** access THEN management-account warning logic SHALL NOT apply.

### Requirement 4

**User Story:** As an approver in Teams, I want the same separation and warning on the Adaptive Card.

#### Acceptance Criteria

1. WHEN building the account access approval Adaptive Card THEN the `FactSet` SHALL list account name and account ID as separate facts (not a single `name (id)` string).
2. WHEN the requested account is the management account THEN the card body SHALL include a visible warning block above the card actions (e.g. `TextBlock` with bold text and/or a `Container` with `attention` style).
3. WHEN the card is updated after a decision THEN existing body content including the warning and facts SHALL remain unless explicitly redesigned (deepcopy-based updates preserve structure).

### Requirement 5

**User Story:** As a deployer, I want the requester Lambda IAM policy to allow the new Organizations read.

#### Acceptance Criteria

1. WHEN Terraform applies the requester Lambda role THEN it SHALL allow `organizations:DescribeOrganization` on `*` alongside existing Organizations list/describe permissions where account UI runs.
2. WHEN the revoker Lambda rebuilds Teams account approval cards THEN its role SHALL allow `organizations:DescribeOrganization` if that code path calls `get_management_account_id`.

### Requirement 6

**User Story:** As a developer, I want automated tests that mock AWS and assert Slack/Teams structures.

#### Acceptance Criteria

1. WHEN testing management detection THEN tests SHALL mock the Organizations client and cover match, non-match, and API failure paths.
2. WHEN testing presentation THEN tests SHALL assert Slack block content or Adaptive Card JSON for management vs member accounts.
