module "audit_bucket" {
  count   = var.s3_name_of_the_existing_bucket == "" ? 1 : 0
  source  = "fivexl/account-baseline/aws//modules/s3_baseline"
  version = "1.5.0"

  bucket_name = local.s3_bucket_name

  versioning = {
    enabled    = true
    mfa_delete = var.s3_mfa_delete
  }

  object_lock_enabled = var.s3_object_lock

  object_lock_configuration = var.s3_object_lock ? var.s3_object_lock_configuration : null
  logging                   = var.s3_logging
}

module "config_bucket" {
  source  = "fivexl/account-baseline/aws//modules/s3_baseline"
  version = "1.5.0"

  bucket_name = local.config_bucket_name

  versioning = {
    enabled    = true
    mfa_delete = false
  }

  object_lock_enabled = false

  server_side_encryption_configuration = var.config_bucket_kms_key_arn != null ? {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm     = "aws:kms"
        kms_master_key_id = var.config_bucket_kms_key_arn
      }
    }
    } : {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  lifecycle_rule = [
    {
      id      = "expire-old-versions"
      enabled = true

      noncurrent_version_expiration = {
        noncurrent_days = 7
      }
    }
  ]
  logging = var.s3_logging

  tags = var.tags
}

resource "aws_s3_object" "approval_config" {
  bucket = module.config_bucket.s3_bucket_id
  key    = "config/approval-config.json"
  content = jsonencode({
    statements       = var.config
    group_statements = var.group_config
  })
  content_type = "application/json"

  server_side_encryption = var.config_bucket_kms_key_arn != null ? "aws:kms" : "AES256"
  kms_key_id             = var.config_bucket_kms_key_arn
}
