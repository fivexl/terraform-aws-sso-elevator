module "dynamodb_table_requests" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "1.2.2"

  name     = "${local.name}-audit-trail"
  hash_key = "request_id"
  range_key = "timestamp"

  attributes = [
    {
      name = "request_id"
      type = "S"
    },
    {
      name = "time"
      type = "S"
    },
    {
      name = "timestamp"
      type = "N"
    },
    {
      name = "account_id"
      type = "S"
    },
    {
      name = "role_name"
      type = "S"
    },
    {
      name = "approver_email"
      type = "S"
    },
    {
      name = "requester_email"
      type = "S"
    }
  ]

  local_secondary_indexes = [
    {
      name               = "SortByTime"
      hash_key           = "request_id"
      range_key          = "time"
      projection_type    = "ALL"
    },
    {
      name               = "SortByAccountId"
      hash_key           = "request_id"
      range_key          = "account_id"
      projection_type    = "ALL"
    },
    {
      name               = "SortByRequester"
      hash_key           = "request_id"
      range_key          = "requester_email"
      projection_type    = "ALL"
    },
    {
      name               = "SortByApprover"
      hash_key           = "request_id"
      range_key          = "approver_email"
      projection_type    = "ALL"
    },
    {
      name               = "SortByRoleName"
      hash_key           = "request_id"
      range_key          = "role_name"
      projection_type    = "ALL"
    }
  ]

  server_side_encryption_enabled = true

  point_in_time_recovery_enabled = true

  tags = var.tags
}
