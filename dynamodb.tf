resource "aws_dynamodb_table" "sso_elevator_cache" {
  count = var.cache_ttl_minutes > 0 ? 1 : 0

  name         = var.cache_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "cache_key"
  range_key    = "item_id"

  attribute {
    name = "cache_key"
    type = "S"
  }

  attribute {
    name = "item_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Enable point-in-time recovery for backup compliance
  point_in_time_recovery {
    enabled = true
  }

  # Enable server-side encryption
  # Uses AWS managed key by default, or custom CMK if provided
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.cache_kms_key_arn
  }

  # Enable deletion protection
  deletion_protection_enabled = true

  tags = merge(
    var.tags,
    {
      Name = var.cache_table_name
    }
  )
}
