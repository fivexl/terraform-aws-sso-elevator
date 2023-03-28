locals {
  revoker_lambda_name   = "${var.revoker_lambda_name}${var.revoker_lambda_name_postfix}"
  requester_lambda_name = "${var.requester_lambda_name}${var.requester_lambda_name_postfix}"
}
