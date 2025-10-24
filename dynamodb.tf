resource "aws_dynamodb_table" "sso_elevator_cache" {
  name           = var.cache_table_name
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "cache_key"
  range_key      = "item_id"

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

  tags = merge(
    var.tags,
    {
      Name = var.cache_table_name
    }
  )

  lifecycle {
    prevent_destroy = false
  }
}
