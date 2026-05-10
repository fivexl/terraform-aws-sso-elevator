# Cross-variable validation: required credentials depend on chat_platform.
check "validate_chat_platform" {
  assert {
    condition     = var.chat_platform != "slack" || (var.slack_signing_secret != "" && var.slack_bot_token != "" && var.slack_channel_id != "")
    error_message = "When chat_platform is \"slack\", slack_signing_secret, slack_bot_token, and slack_channel_id must be set (non-empty)."
  }
  assert {
    condition     = var.chat_platform != "teams" || (var.teams_microsoft_app_id != "" && var.teams_microsoft_app_password != "" && var.teams_approval_conversation_id != "")
    error_message = "When chat_platform is \"teams\", teams_microsoft_app_id, teams_microsoft_app_password, and teams_approval_conversation_id must be set (non-empty)."
  }
}
