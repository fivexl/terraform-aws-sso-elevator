locals {
  sso_elevator_config = jsonencode(
    merge(
      var.config,
      {
        "sso_instance:" : var.identity_provider_arn # TODO: remove ':' from key
        "users" : [
          for user in var.config.users :
          merge(user, { "sso_id" : data.aws_identitystore_user.all[user.email].user_id })
        ]
        "permission_sets" : [
          for permission_set in var.config.permission_sets :
          merge(permission_set, { "arn" : data.aws_ssoadmin_permission_set.all[permission_set.name].arn })
        ]
      }
    )
  )
}
