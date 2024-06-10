module "audit_bucket" {
  count   = var.s3_name_of_the_existing_bucket == "" ? 1 : 0
  source  = "fivexl/account-baseline/aws//modules/s3_baseline"
  version = "1.3.2"
  
  bucket_name = local.s3_bucket_name

  versioning = {
    enabled    = true
    mfa_delete = var.s3_mfa_delete
  }

  object_lock_enabled = var.s3_object_lock

  object_lock_configuration = var.s3_object_lock ? var.s3_object_lock_configuration : null
  logging = var.s3_logging
}
