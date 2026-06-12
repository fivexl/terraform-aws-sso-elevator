variable "teams_azure_tenant_id" {
  description = "Azure AD / Entra tenant (directory) ID. Found in Entra admin center → Overview."
  type        = string
}

variable "teams_approval_conversation_id" {
  description = <<EOT
Bot Framework conversation.id for the Teams channel where approval Adaptive Cards are posted.
Decode the URL-encoded channel ID from the Teams channel link, e.g.:
  19%3A280e28c1c2e342dc8fff6d3a495e8d9a%40thread.tacv2  →  19:280e28c1c2e342dc8fff6d3a495e8d9a@thread.tacv2
See the "Obtaining teams_approval_conversation_id" section in the root README.
EOT
  type        = string
}
