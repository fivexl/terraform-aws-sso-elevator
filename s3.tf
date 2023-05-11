resource "aws_s3_bucket" "logs" {
  count  = var.name_of_existing_s3_bucket == "" ? 1 : 0
  bucket = local.s3_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "versioning_example" {
  count  = var.name_of_existing_s3_bucket == "" ? 1 : 0
  bucket = aws_s3_bucket.logs[count.index].id

  versioning_configuration {
    status = "Enabled"
  }
}
