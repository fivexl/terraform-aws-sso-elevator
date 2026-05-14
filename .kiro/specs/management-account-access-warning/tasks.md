# Implementation Tasks: Management account highlighting

## Tasks

- [x] 1. Add Kiro spec documents under `.kiro/specs/management-account-access-warning/`
- [x] 2. Organizations module: `get_management_account_id`, `is_management_account` + exception logging
- [x] 3. Terraform: `organizations:DescribeOrganization` on requester Lambda IAM policy
- [x] 4. Slack: account dropdown labels; `build_approval_request_message_blocks` field split + warning before buttons; wire `slack_app` load + submit paths
- [x] 5. Teams: `build_account_access_form` and `build_approval_card`; wire `teams_handlers`, `teams_approval_deferred`, `revoker` account paths
- [x] 6. Unit tests (organizations + Teams card structure; adjust existing property tests for new fact titles)
