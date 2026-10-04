# Releasing a new version

One stable bare SemVer tag releases the Terraform module, [Elevator CLI](cmd/elevator/README.md), Homebrew cask, and Lambda images under the same version. GitHub Actions creates the release and its generated notes; do not create a GitHub Release manually.

1. Set `ecr_repo_tag`'s default in `vars.tf` to the next version and merge the release-ready commit to `main`.
2. Confirm `main` is green and the release repository has these Actions secrets: `APP_ID`, `APP_PRIVATE_KEY`, `MACOS_SIGN_P12`, `MACOS_SIGN_PASSWORD`, `MACOS_NOTARY_ISSUER_ID`, `MACOS_NOTARY_KEY_ID`, and `MACOS_NOTARY_KEY`. The GitHub App must be installed only on `fivexl/homebrew-tap` with Contents read/write permission.
3. Create and push the matching stable tag, for example `4.4.3`. Prerelease tags and `elevator-v*` tags are not supported.
4. Watch **CLI Release**. It verifies that the tag is on `main`, exercises Apple signing/notarization without publishing, runs the repository and CLI checks, then publishes and smoke-tests the signed binaries and Homebrew cask.
5. A successful binary release starts **Build and Push Docker Images** asynchronously for `requester-X.Y.Z`, `revoker-X.Y.Z`, and `attribute-syncer-X.Y.Z`. The GitHub release can be available briefly before those images finish pushing.

Pull requests from branches in this repository also run **Build and Push Docker Images** and publish `requester-pr-<N>-<sha>`, `revoker-pr-<N>-<sha>`, and `attribute-syncer-pr-<N>-<sha>`; set `ecr_repo_tag = "pr-<N>-<sha>"` to test one (the run summary shows the value). Fork PRs get no secrets and publish nothing. Every push to `main` publishes `requester-main`, `revoker-main`, and `attribute-syncer-main`; set `ecr_repo_tag = "main"` to test the latest merge to `main`. The tag moves with every merge, and superseded `main` images expire automatically, as do PR images.

An Apple error containing `FORBIDDEN.REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED` requires the Apple Account Holder to accept the pending agreement in App Store Connect, then re-run the failed jobs. Never move a successfully published tag; release corrections under the next patch version.

ECR is private for the following reasons:

- AWS Lambda can't use any other source of images except ECR.
- AWS Lambda can't use public ECR.
- AWS Lambda doesn't support pulling container images from Amazon ECR using a pull-through cache rule (so we can't create a private repo from the user's side to pull images from the GHCR, for example).

Images and repositories are replicated in every region that AWS SSO supports except these:
```
- ap_east_1
- eu_south_1
- ap_southeast_3
- af_south_1
- me_south_1
- il_central_1
- me_central_1
- eu_south_2
- ap_south_2
- eu_central_2
- ap_southeast_4
- ca_west_1
- us_gov_east_1
- us_gov_west_1
```
Those regions are not enabled by default. If you need to use a region that is not supported by the module, please let us know by creating an issue, and we will add support for it. 
