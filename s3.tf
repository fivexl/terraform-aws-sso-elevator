resource "aws_s3_bucket" "logs" {
  bucket = local.s3_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "versioning_example" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}
