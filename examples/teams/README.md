# Microsoft Teams Example

This example deploys SSO Elevator with Microsoft Teams as the chat platform.

## Prerequisites

1. Complete the **Microsoft Teams app creation** steps in the [root README](../../README.md#microsoft-teams-app-creation).
2. Store credentials in AWS SSM Parameter Store:
   - `/sso-elevator/teams-app-id` — bot Application (client) ID
   - `/sso-elevator/teams-app-password` — bot client secret (use `SecureString`)
3. Provide the required input variables (see below).

## Deployment workflow

```bash
# 1. Deploy infrastructure (use dummy SSM values if Teams app isn't created yet)
terraform init
terraform apply -var="teams_azure_tenant_id=<tenant-id>" \
                -var="teams_approval_conversation_id=<conversation-id>"

# 2. Copy requester_api_endpoint_url output into Teams Developer Portal
#    as the bot messaging endpoint, then save.

# 3. Smoke test: in Teams, run /access or /group-access
```

## Inputs

| Name | Description | Required |
|------|-------------|:--------:|
| `teams_azure_tenant_id` | Entra tenant (directory) ID | yes |
| `teams_approval_conversation_id` | Bot Framework `conversation.id` for the approval channel | yes |

## Outputs

| Name | Description |
|------|-------------|
| `requester_api_endpoint_url` | Set as bot messaging endpoint in Teams Developer Portal |
| `elevator_requests_table_name` | DynamoDB table holding access request state |
