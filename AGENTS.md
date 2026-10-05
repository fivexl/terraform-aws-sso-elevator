# Agent instructions

## Code Quality & Philosophy

### Simplicity First
- Prioritize simplicity and readability over exhaustive validation
- Achieve goals with minimal code - less is more
- Actively identify and remove unused or dead code
- Do not maintain backward compatibility - remove obsolete code paths

### Python Environment
- Always use `uv run python` instead of direct `python` commands

### Exception Logging
- Use `logger.exception()` instead of `logger.error()` for exceptions
- Always include the exception object in the log message
- Example: `logger.exception(f"Text describing what happened: {error}")`
- Exception: for an exception object that was just constructed rather than
  caught from an active `except` block (so there is no real traceback for
  `logger.exception()`'s `sys.exc_info()` to attach — it would log a
  misleading "NoneType: None" trace instead), use `logger.error()` and note
  in a comment why there's no active exception context to attach.

## Configuration Management

Wire a new configuration parameter through `vars.tf`, the
`environment_variables` of each Lambda that reads it (`*_lambda.tf`), and
`src/config.py` (or `src/sync_config.py` for the attribute syncer).

## External Tools & Resources
- Use AWS knowledge MCP tools for AWS service documentation and best practices

## Testing

### Running Tests
- Execute tests using: `bash run-tests.sh`
- Always run pre-commit hooks: `git add . && pre-commit run -a`
- Re-run full test suite after completing any task to ensure integrity

### Mocking Strategy
- Mock all external dependencies including:
  - AWS services
  - Slack Bolt

## Version Control

### Git Commands
- Always use `--no-pager` with git commands to prevent interactive paging: `git --no-pager log`, `git --no-pager diff`
- Use heredoc for commit messages and PR descriptions:
  ```bash
  git commit -S -m "$(cat <<'EOF'
  [fix] commit title

  Detailed commit message here
  EOF
  )"
  ```

### Branching
- Start a new branch from `main` for every change

### Commit Workflow
- Commit all changes at the end of every task
- Commit title format: `[fix/feature/refactoring] task name`
- Commit message: Include detailed task summary
- Always sign commits

## Releases

- Stable bare SemVer tags such as `4.4.3` are shared by the Terraform module,
  Elevator CLI, Homebrew cask, and Lambda images. Do not use `elevator-v*`, a
  leading `v`, or prerelease tags.
- Prepare releases on `main` and keep `vars.tf`'s `ecr_repo_tag`, generated
  README documentation, and the intended tag on the same version.
- Add the version's `CHANGELOG.md` entry in the release PR.
- Pushing the tag is the only release trigger. GitHub Actions creates the
  release and generated notes; do not create a GitHub Release manually.
- Never create, move, delete, or push a release tag without the user's explicit
  authorization. Agents may prepare and verify a release, but the maintainer
  owns the publication trigger.
- macOS releases must remain signed and notarized by Apple Team ID
  `T962D4K3Y7`. Never make signing conditional or allow missing secrets to
  degrade into an unsigned release.
- The maintainer procedure is in `docs/releasing.md`.

## Docs

- Never rename or delete a `docs/` page or an `UPGRADE-*.md` file: Terraform
  Registry pages for old tags link to them on `main`. Replace a moved page with
  a stub that links to the new place.
- `README.md` and `cmd/elevator/README.md` link to other repo files with
  absolute `https://github.com/fivexl/terraform-aws-sso-elevator/blob/main/...`
  URLs, because the Registry and pkg.go.dev don't rewrite relative links.
  Links between `docs/` pages are relative.
- Don't edit the README between `<!-- BEGIN_TF_DOCS -->` and
  `<!-- END_TF_DOCS -->`; pre-commit regenerates it from `vars.tf` and
  `outputs.tf`.

## Dependency updates

New upstream versions soak before they ship, as a defence against package and
repository takeovers: 14 days from registry publish, 7 days for a fix to a
critical/high advisory.

- Dependabot opens security PRs only for `uv` (`src`, `layer`) and `gomod`
  (`cmd/elevator`). They are signals, not merge targets: security PRs skip
  Dependabot's cooldown and edit only one of `uv.lock`/`requirements.txt`.
  Actions, Terraform, Docker and pre-commit get one grouped monthly PR with a
  14-day cooldown; majors open individually.
- Dependency changes reach `main` only through a release PR checked by
  `scripts/deps_ready.py`. Close Dependabot PRs by hand, naming the version
  taken.
- `[tool.uv] exclude-newer = "14 days"` enforces the soak in `uv lock`. It needs
  uv >= 0.12.17 locally; older uv silently ignores it (`required-version`
  makes it fail instead).

Release-branch procedure, in order:

1. `uv run python scripts/deps_ready.py` — security rows with the newest soaked
   fix, open routine Dependabot PRs, and manual pins (Go toolchain, syft,
   GoReleaser).
2. Per directory: `uv lock --upgrade-package <pkg>==<ver>`. For a 7–13-day-old
   critical/high fix, first add `exclude-newer-package = { <pkg> = "<that
   version's upload time>" }` under `[tool.uv]`.
3. `go get <module>@<ver>` in `cmd/elevator`, and pin edits (action SHAs,
   hook revs, image digests, Terraform `version =`, `toolchain`).
4. `git add . && pre-commit run -a` (re-exports both `requirements.txt`).
5. `bash run-tests.sh`.
6. `uv run python scripts/deps_ready.py verify` — every version changed since
   the last release tag must pass; `OVERRIDE` rows go in the PR body.

Do not tag while the report shows a READY critical/high row that is not applied.
List BLOCKED, no-fix, medium/low and dev rows in the release PR body.

Overrides under 7 days need evidence in the release PR body: package, exact
version, lockfile hash (wheel sha256 in `uv.lock`, `go.sum` line or action
commit SHA), and what was verified — the PyPI attestation
`https://pypi.org/integrity/<pkg>/<ver>/<file>/provenance` from the project's
own trusted-publisher repo and workflow, or a signed tag on the expected repo
for Go modules and actions. Scope it with an `exclude-newer-package` entry set
to that version's upload time. Nothing verifiable means wait.

Remove an `exclude-newer-package` entry once its version is more than 14 days
old.
