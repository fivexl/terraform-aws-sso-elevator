module "sso_elevator_bucket" {
  count   = var.s3_name_of_the_existing_bucket == "" ? 1 : 0
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
    mfa_delete = var.s3_mfa_delete
  }

  object_lock_enabled = var.s3_object_lock

  object_lock_configuration = var.s3_object_lock ? var.s3_object_lock_configuration : null

  logging = var.s3_logging

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

