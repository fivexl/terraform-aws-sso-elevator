# Start using s3 baseline for audit bucket
moved {
  from = module.sso_elevator_bucket[0]
  to   = module.audit_bucket[0].module.bucket_baseline[0]
}
