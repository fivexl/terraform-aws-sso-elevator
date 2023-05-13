module "sso_elevator_bucket" {
  count   = var.name_of_existing_s3_bucket == "" ? 1 : 0
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "3.10.1"

  bucket = local.s3_bucket_name

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  versioning = {
    enabled    = true
    mfa_delete = var.mfa_delete
  }

  logging = {
    target_bucket = var.name_of_logging_bucket_for_s3 != "" ? var.name_of_logging_bucket_for_s3 : local.s3_bucket_name
    target_prefix = var.name_of_logging_bucket_for_s3 != "" ? "" : "s3_access_logs/"
  }

  object_lock_enabled = var.object_lock_for_s3_bucket

  object_lock_configuration = var.object_lock_for_s3_bucket ? {
    rule = {
      default_retention = {
        mode  = "GOVERNANCE"
        years = 2
      }
    }
  } : null

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
