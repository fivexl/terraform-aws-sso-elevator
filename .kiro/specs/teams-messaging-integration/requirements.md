# Requirements Document: Microsoft Teams Integration for SSO Elevator

## Introduction

SSO Elevator is an application for managing temporary access to AWS SSO (Identity Center) via a messaging platform. Currently, the application works exclusively through Slack: users request access to AWS accounts and groups, approvers approve or discard requests, and the system automatically revokes permissions when they expire.

The goal of this feature is to adapt the existing SSO Elevator workflow for Microsoft Teams while preserving identical behavior. Business logic (access_control, SSO operations, revocation scheduling) remains unchanged — only the transport and UI layer changes: Block Kit → Adaptive Cards, Slack Bolt → Microsoft Teams SDK for Python, Slack Web API → Teams / Bot Connector APIs + Microsoft Graph.

## Glossary

- **SSO_Elevator**: Application for managing temporary access to AWS SSO via a messaging platform
- **Teams_Bot**: Microsoft Teams SDK for Python `App` and registered handlers that process incoming messages, dialog (task), and card action activities from Microsoft Teams; logic in `teams_handlers.py` and related `teams_*.py` modules (parallel driver, not an abstraction over Slack)
- **Adaptive_Card**: Microsoft Teams UI card format (equivalent of Block Kit in Slack)
- **Task_Module**: Pop-up dialog window in Teams for data input (equivalent of Slack Modal)
- **Teams_Python_SDK**: Microsoft Teams SDK for Python packages (`microsoft-teams-apps`, `microsoft-teams-api` from the [teams.py](https://github.com/microsoft/teams.py) monorepo)
- **Microsoft_Graph_API**: Microsoft REST API for retrieving user information from Entra ID (Azure AD)
- **Revoker**: Lambda function that processes scheduled permission revocation events and notifications; selects Slack or Teams transport at runtime based on `chat_platform`
- **Parallel_Driver**: Architectural pattern used in this integration — `teams_handler.py` mirrors `main.py`/`group.py` for Teams, calling the same business logic functions. No shared abstraction layer over Slack and Teams exists; the existing Slack code is untouched.
- **ConversationReference**: Bot Framework object that stores conversation context for sending proactive messages
- **Activity**: Unit of data exchange in Bot Framework (message, invoke, update)
- **Config**: Application configuration module (`config.py`) that defines parameters via environment variables; performs runtime validation of platform-specific required fields

## Requirements

### Requirement 1: Entry Point — Bot Commands for Access Requests

**User Story:** As a Teams user, I want to initiate a request for access to an AWS account or group via a bot command, so that I can obtain temporary permissions without switching to Slack.

#### Acceptance Criteria

1. WHEN a user sends the `/access` command to the bot in Teams, THE Teams_Bot SHALL open a Task_Module with an AWS account access request form
2. WHEN a user sends the `/group-access` command to the bot in Teams, THE Teams_Bot SHALL open a Task_Module with a group access request form
3. WHEN the `/access` command is received and statements are not configured, THE Teams_Bot SHALL send a message to the approval channel indicating that statements are not configured
4. WHEN the `/group-access` command is received and group_statements are not configured, THE Teams_Bot SHALL send a message to the approval channel indicating that group_statements are not configured
5. WHEN a command is received from a user who does not exist in AWS SSO (Identity Center), THE Teams_Bot SHALL send an SSOUserNotFound error message to the approval channel with a mention of the user

### Requirement 2: Access Request Forms via Adaptive Cards in Task Module

**User Story:** As a Teams user, I want to fill out an access request form in a dialog window (Task Module), so that I can specify the account, role, duration, and reason for the request.

#### Acceptance Criteria

1. WHEN the Task_Module for account access request is opened, THE Teams_Bot SHALL display an Adaptive_Card with fields: account selection (Input.ChoiceSet), permission set selection (Input.ChoiceSet), duration selection (Input.ChoiceSet), and reason (Input.Text with isMultiline)
2. WHEN the Task_Module for group access request is opened, THE Teams_Bot SHALL display an Adaptive_Card with fields: group selection (Input.ChoiceSet), duration selection (Input.ChoiceSet), and reason (Input.Text with isMultiline)
3. WHEN account and permission set data is loaded for the form, THE Teams_Bot SHALL use the same data sources (organizations, sso) as the Slack handler
4. WHEN a user submits the completed form (task/submit), THE Teams_Bot SHALL parse the data from the Adaptive_Card and pass it to the existing access_control business logic

### Requirement 3: Approval Request Message in Teams Channel

**User Story:** As an approver, I want to see access requests in the Teams channel with Approve/Discard buttons, so that I can make decisions on requests.

#### Acceptance Criteria

1. WHEN an account access request form is submitted, THE Teams_Bot SHALL post an Adaptive_Card to the Teams approval channel with information: requester name, account, role, reason, duration
2. WHEN a group access request form is submitted, THE Teams_Bot SHALL post an Adaptive_Card to the Teams approval channel with information: requester name, group, reason, duration
3. WHEN a request requires approval (DecisionReason.RequiresApproval), THE Adaptive_Card SHALL contain Action.Submit buttons "Approve" (style: positive) and "Discard" (style: destructive) with request data in the data field
4. WHEN a request does not require approval (ApprovalNotRequired or SelfApproval), THE Adaptive_Card SHALL be displayed without approval buttons
5. THE Adaptive_Card for the request SHALL contain status color indication via Container or TextBlock styles (equivalent of emoji color coding in Slack)
6. WHEN a request is posted, THE Teams_Bot SHALL send a reply in the thread (reply chain) mentioning approvers using the `<at>DisplayName</at>` format and Mention objects in entities

### Requirement 4: Approve and Discard Button Handling

**User Story:** As an approver, I want to click the Approve or Discard button on a request card, so that I can approve or reject an access request.

#### Acceptance Criteria

1. WHEN an approver clicks the "Approve" button on an Adaptive_Card, THE Teams_Bot SHALL pass the data from Action.Submit.data to the existing access_control.make_decision_on_approve_request logic
2. WHEN an approver clicks the "Discard" button on an Adaptive_Card, THE Teams_Bot SHALL update the request status to discarded and update the card
3. WHEN a button decision is made, THE Teams_Bot SHALL update the Adaptive_Card via update_activity: remove the ActionSet with buttons and add information about the decision made
4. WHEN a button decision is made, THE Teams_Bot SHALL update the card's color indication according to the result (approved — green, discarded — red)
5. WHEN an approver does not have permission to approve the request (decision.permit is False), THE Teams_Bot SHALL send a reply in the thread with a message about the inability to approve
6. WHEN a request is already being processed (in-flight approval), THE Teams_Bot SHALL send a message indicating that the request is already being processed
7. THE Teams_Bot SHALL pass structured request data via Action.Submit.data (JSON) without parsing text from message blocks

### Requirement 5: User Identity Resolution via Microsoft Graph

**User Story:** As a system, I want to determine the email and name of a Teams user via Microsoft Graph API, so that I can link the Teams user to an AWS SSO user.

#### Acceptance Criteria

1. WHEN the Teams_Bot receives an activity from a user, THE Teams_Bot SHALL extract the aad_object_id from activity.from_property and obtain the user's email via TeamsInfo or Microsoft_Graph_API
2. WHEN it is necessary to find a user by email (for approver lookup), THE Teams_Bot SHALL use Microsoft_Graph_API with a filter on the mail field
3. WHEN Microsoft_Graph_API returns a 429 error (rate limit), THE Teams_Bot SHALL retry the request respecting the Retry-After header
4. THE Teams_Bot SHALL create a user object compatible with the existing entities.slack.User model (id, email, real_name/display_name) for passing to business logic

### Requirement 6: Direct Messages (DM) to Users via Proactive Messages

**User Story:** As a Teams user, I want to receive notifications about my request status in a direct message, if I am not a member of the approval channel.

#### Acceptance Criteria

1. WHILE the send_dm_if_user_not_in_channel parameter is enabled, WHEN a user is not a member of the Teams approval channel, THE Teams_Bot SHALL send a proactive direct message to the user about the request status
2. WHEN the Teams_Bot sends a proactive message, THE Teams_Bot SHALL use a stored ConversationReference to create the conversation
3. IF sending a proactive message fails with a 403 error (Forbidden), THEN THE Teams_Bot SHALL log the error and continue operation without interrupting the main flow
4. WHEN a user interacts with the bot for the first time, THE Teams_Bot SHALL save the ConversationReference for subsequent proactive message delivery

### Requirement 7: Revoker Integration for Teams Notifications

**User Story:** As a system, I want to send permission revocation notifications and approver reminders via Teams, when the platform is configured for Teams.

#### Acceptance Criteria

1. WHILE the chat_platform parameter is set to "teams", WHEN the Revoker processes a ScheduledRevokeEvent, THE Revoker SHALL send a permission revocation notification via Bot Framework REST API instead of Slack Web API
2. WHILE the chat_platform parameter is set to "teams", WHEN the Revoker processes a DiscardButtonsEvent, THE Revoker SHALL update the Adaptive_Card via update_activity: remove buttons and add text about request expiration
3. WHILE the chat_platform parameter is set to "teams", WHEN the Revoker processes an ApproverNotificationEvent, THE Revoker SHALL send a reminder in the thread (reply chain) via Bot Framework REST API
4. WHILE the chat_platform parameter is set to "teams" and post_update_to_slack is enabled, WHEN permission revocation occurs, THE Revoker SHALL send a notification to the Teams approval channel. Note: the flag `post_update_to_slack` applies to both platforms — for Teams it means "send a revocation notification to the approval channel"; the flag name is not changed in this scope.
5. THE Revoker SHALL use the same EventBridge Scheduler mechanism for scheduling events, changing only the notification delivery method

### Requirement 8: Teams Platform Configuration

**User Story:** As an administrator, I want to configure SSO Elevator to work with Teams via environment variables, so that I can switch the platform without code changes.

#### Acceptance Criteria

1. THE Config SHALL support a chat_platform parameter with values "slack" and "teams" for selecting the messaging platform
2. WHEN chat_platform is set to "teams", THE Config SHALL require parameters: teams_microsoft_app_id, teams_microsoft_app_password, teams_azure_tenant_id, teams_approval_conversation_id — validated at Lambda startup via a Pydantic `model_validator` in `config.py` (runtime validation); Terraform does not enforce this constraint to allow flexible deployment
3. WHEN chat_platform is set to "slack", THE Config SHALL require parameters: slack_bot_token, slack_channel_id (current behavior)
4. THE Config SHALL support a teams_approval_conversation_id parameter for specifying the approval channel in Teams (equivalent of slack_channel_id)

### Requirement 9: Lambda Handler for Teams Bot

**User Story:** As a system, I want to process incoming HTTP requests from Azure Bot Service via AWS Lambda, so that the Teams bot runs on the same infrastructure as the Slack handler.

#### Acceptance Criteria

1. THE Teams_Bot SHALL provide a lambda_handler function that accepts HTTP events from API Gateway and passes them to the Bot Framework SDK for processing
2. WHEN an incoming request is received, THE Bot_Framework_SDK SHALL automatically validate the JWT token from Azure Bot Service
3. THE Teams_Bot SHALL use ActivityHandler for routing incoming activities (on_message_activity, on_invoke_activity)
4. WHEN request processing takes more than 5 seconds (AWS SSO operations), THE Teams_Bot SHALL return an immediate response and process the heavy work asynchronously, updating the card upon completion

### Requirement 10: Message Updates and Card State Management

**User Story:** As a system, I want to update previously sent cards in Teams (color, buttons, status), so that they reflect the current state of the request.

#### Acceptance Criteria

1. WHEN a decision on a request is made, THE Teams_Bot SHALL update the original Adaptive_Card via update_activity using the stored activity_id
2. WHEN a request expires (DiscardButtonsEvent), THE Teams_Bot SHALL update the Adaptive_Card: remove buttons and set the "expired" style
3. THE Teams_Bot SHALL store the activity_id and conversation_id of sent messages for subsequent updates (equivalent of channel_id + message_ts in Slack)
4. WHEN an activity update fails with an error, THE Teams_Bot SHALL log the error and continue operation

### Requirement 11: User Channel Membership Check in Teams

**User Story:** As a system, I want to check whether a user is a member of the approval channel in Teams, so that I can decide whether to send a direct message.

#### Acceptance Criteria

1. WHEN it is necessary to determine whether a user is a member of the approval channel, THE Teams_Bot SHALL use TeamsInfo.get_team_members or an equivalent Bot Framework method to retrieve the member list
2. IF retrieving the member list fails with an error, THEN THE Teams_Bot SHALL consider the user as not being a channel member and send a DM

### Requirement 12: User Mentions in Teams Messages

**User Story:** As an approver, I want to receive @mentions in request messages, so that I get Teams notifications about new requests.

#### Acceptance Criteria

1. WHEN a message contains a user mention, THE Teams_Bot SHALL use the `<at>DisplayName</at>` format in the text and add a Mention object to the activity's entities array
2. WHEN it is necessary to mention a user by SSO principal ID (in Revoker notifications), THE Teams_Bot SHALL resolve the principal ID to a Teams user via email and Microsoft_Graph_API

### Requirement 13: Audit Logging with Teams Support

**User Story:** As an administrator, I want to see Teams user identifiers in audit logs, so that I can track actions regardless of the platform.

#### Acceptance Criteria

1. WHEN a request is processed via Teams, THE SSO_Elevator SHALL write the Teams user identifier to the audit log (S3) in the requester_slack_id and approver_slack_id fields (for backward compatibility) or in new platform-independent fields
2. THE SSO_Elevator SHALL store the user's email in the audit log regardless of the platform (requester_email, approver_email)
