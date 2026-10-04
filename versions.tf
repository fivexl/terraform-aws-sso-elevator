terraform {
  # Terraform 1.11 is the first with write-only arguments, which slack_ssm_secrets.tf relies on
  # to keep the Slack secrets out of state. aws 6.0 adds data.aws_region's `region` attribute.
  required_version = ">= 1.11.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
    external = {
      source  = "hashicorp/external"
      version = ">= 1.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 1.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
  }
}
