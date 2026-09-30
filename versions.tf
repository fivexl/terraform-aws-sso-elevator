terraform {
  # >= 1.11.0 (not just "~> 1.0"), and the aws provider floor bumped to >= 5.87.0 below:
  # both are required for write-only arguments (slack_ssm_secrets.tf's aws_ssm_parameter
  # resources use value_wo/value_wo_version), which is what lets Terraform pre-create those
  # parameters for an operator without ever persisting the real secret value to state --
  # write-only support for aws_ssm_parameter specifically shipped in provider v5.87.0. This
  # is a genuine breaking change for anyone on an older Terraform/provider, not something to
  # silently widen later.
  required_version = ">= 1.11.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.87.0"
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
