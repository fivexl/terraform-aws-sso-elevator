# Optional WAF on the requester REST API stage: a module-created web ACL (waf_enabled) or an
# operator-owned one (waf_web_acl_arn). Leave both unset when Firewall Manager associates one.

locals {
  waf_web_acl_arn = var.waf_enabled ? aws_wafv2_web_acl.requester[0].arn : var.waf_web_acl_arn
}

resource "aws_wafv2_web_acl" "requester" {
  count       = var.waf_enabled ? 1 : 0
  name        = var.api_gateway_name
  description = "SSO Elevator access-requester API"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  # Sampling is off on the ACL and every rule: log redaction does not cover sampled requests, which
  # would show the auth and Slack signature headers to anyone allowed to view samples.

  # Keeps one noisy IP from draining the API Gateway throttle shared by all callers.
  rule {
    name     = "RateLimitPerIp"
    priority = 0

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.waf_rate_limit
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.api_gateway_name}-RateLimitPerIp"
      sampled_requests_enabled   = false
    }
  }

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"

        # Slack view_submission bodies can exceed the rule's 8 KB limit.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.api_gateway_name}-AWSManagedRulesCommonRuleSet"
      sampled_requests_enabled   = false
    }
  }

  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.api_gateway_name}-AWSManagedRulesKnownBadInputsRuleSet"
      sampled_requests_enabled   = false
    }
  }

  rule {
    name     = "AWSManagedRulesAmazonIpReputationList"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.api_gateway_name}-AWSManagedRulesAmazonIpReputationList"
      sampled_requests_enabled   = false
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.api_gateway_name
    sampled_requests_enabled   = false
  }

  tags = var.tags
}

resource "aws_wafv2_web_acl_association" "requester" {
  count        = var.waf_enabled || var.waf_web_acl_arn != null ? 1 : 0
  resource_arn = aws_api_gateway_stage.requester.arn
  web_acl_arn  = local.waf_web_acl_arn

  lifecycle {
    # A precondition rather than a cross-variable validation, which needs Terraform >= 1.9.
    precondition {
      condition     = !(var.waf_enabled && var.waf_web_acl_arn != null)
      error_message = "Set either waf_enabled = true (module-created web ACL) or waf_web_acl_arn (your own web ACL), not both."
    }
  }
}

# WAF requires the log group name to start with "aws-waf-logs-".
resource "aws_cloudwatch_log_group" "waf" {
  count             = var.waf_enabled ? 1 : 0
  name              = "aws-waf-logs-${var.api_gateway_name}"
  retention_in_days = var.logs_retention_in_days
  tags              = var.tags
}

resource "aws_wafv2_web_acl_logging_configuration" "requester" {
  count                   = var.waf_enabled ? 1 : 0
  resource_arn            = aws_wafv2_web_acl.requester[0].arn
  log_destination_configs = [aws_cloudwatch_log_group.waf[0].arn]

  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
  redacted_fields {
    single_header {
      name = "x-amz-security-token"
    }
  }
  redacted_fields {
    single_header {
      name = "x-slack-signature"
    }
  }
}
