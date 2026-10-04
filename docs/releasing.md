# Releasing a new version

Maintainer procedure. The rules agents follow (tag format, who may push a tag, signing) are in [AGENTS.md "Releases"](../AGENTS.md#releases); dependency soak and the release-branch dependency checks are in [AGENTS.md "Dependency updates"](../AGENTS.md#dependency-updates).

One stable bare SemVer tag (for example `5.0.0`) releases the Terraform module, the [Elevator CLI](../cmd/elevator/README.md), the Homebrew cask and the Lambda images under the same version. GitHub Actions creates the release with GitHub-generated notes; do not create a GitHub Release by hand.

1. In a release PR to `main`: set `ecr_repo_tag`'s default in `vars.tf` to the new version (pre-commit regenerates the README inputs table), and add the version's entry to [CHANGELOG.md](../CHANGELOG.md). Merge it.
2. Confirm `main` is green and the repository has these Actions secrets: `APP_ID`, `APP_PRIVATE_KEY`, `MACOS_SIGN_P12`, `MACOS_SIGN_PASSWORD`, `MACOS_NOTARY_ISSUER_ID`, `MACOS_NOTARY_KEY_ID` and `MACOS_NOTARY_KEY`. The GitHub App must be installed only on `fivexl/homebrew-tap`, with Contents read/write permission.
3. Create and push the tag on a commit reachable from `main`. Push only that tag, never `git push --tags`.
4. Watch **CLI Release**. It rejects a tag that is not bare SemVer, not on `main`, or already a completed release; checks the secrets and exercises Apple signing and notarization without publishing; runs the repository and CLI checks; then publishes the signed binaries and Homebrew cask and smoke-tests them. If a smoke test fails, the release is kept as a prerelease and the previous cask is restored.
5. Only after the smoke tests pass does it promote the release and start **Build and Push Docker Images** for `requester-X.Y.Z`, `revoker-X.Y.Z` and `attribute-syncer-X.Y.Z`. The GitHub release can be visible for a few minutes before those images exist.

An Apple error containing `FORBIDDEN.REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED` means the Apple Account Holder must accept the pending agreement in App Store Connect; then re-run the failed jobs. Never move a published tag; release corrections under the next patch version.

## Test images

Pull requests from branches in this repository also run **Build and Push Docker Images** and publish `requester-pr-<N>-<sha>`, `revoker-pr-<N>-<sha>` and `attribute-syncer-pr-<N>-<sha>`; set `ecr_repo_tag = "pr-<N>-<sha>"` to test one (the run summary shows the value). Fork and Dependabot PRs get no secrets and publish nothing. Every push to `main` publishes `requester-main`, `revoker-main` and `attribute-syncer-main`; set `ecr_repo_tag = "main"` to test the latest merge. That tag moves with every merge. Superseded `main` images and PR images expire automatically.
