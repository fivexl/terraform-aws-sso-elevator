locals {
  sso_elevator_config = jsonencode(
    merge(
      {
        "sso_instance:" : var.identity_provider_arn # TODO: remove ':' from key
      },
      var.config
    )
  )
}
