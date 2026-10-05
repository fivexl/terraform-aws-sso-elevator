# Upgrading to 4.0

This guide takes a 3.x deployment to 4.0. To go on to 5.0, upgrade to 4.4.3 next, then follow [UPGRADE-5.0.md](UPGRADE-5.0.md).

4.0 changes no inputs, outputs or behaviour. It moves the module's internal S3 buckets (audit and config) to `fivexl/account-baseline` `s3_baseline` 2.0.0, which requires hashicorp/aws v6. So the root module must run on hashicorp/aws 6.x.

1. Move the root module to hashicorp/aws 6.x. Set the provider constraint to `>= 6.0` (or `~> 6.0`), and fix your other resources with the provider's [version 6 upgrade guide](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/guides/version-6-upgrade).
2. Bump the module `version` to 4.0.0. You can go straight to 4.4.3 instead. It also brings the 4.4.x changes listed under "From a release before 4.4.3" in [UPGRADE-5.0.md](UPGRADE-5.0.md#breaking-changes).
3. Run `terraform init -upgrade`.
4. Run `terraform plan` and review it. The module's own changes are limited to the S3 buckets and the new Lambda image tag.
5. Run `terraform apply`.
