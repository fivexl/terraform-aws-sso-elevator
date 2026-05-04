# Cross-variable validation: required credentials depend on chat_platform.
resource "null_resource" "validate_chat_platform" {
  triggers = {
    chat_platform = var.chat_platform
  }

  lifecycle {
    precondition {
      condition     = var.chat_platform != "slack" || (var.slack_signing_secret != "" && var.slack_bot_token != "" && var.slack_channel_id != "")
      error_message = "When chat_platform is \"slack\", slack_signing_secret, slack_bot_token, and slack_channel_id must be set (non-empty)."
    }
    precondition {
      condition     = var.chat_platform != "teams" || (var.teams_microsoft_app_id != "" && var.teams_microsoft_app_password != "" && var.teams_approval_conversation_id != "")
      error_message = "When chat_platform is \"teams\", teams_microsoft_app_id, teams_microsoft_app_password, and teams_approval_conversation_id must be set (non-empty)."
    }
  }
}
