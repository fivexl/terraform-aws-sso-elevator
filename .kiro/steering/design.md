# Kiro Steering Rules

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

When adding new configuration parameters:
- Update all relevant shell scripts
- Update Docker files
- Ensure consistency across all runtime environments

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
  - Valkey
  - Slack Bolt
  - Strands Agents

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
- start a new branch of main for every vibe request
- stay on the same branch when working on specs or have vibe requests started from the spec branch

### Commit Workflow
- Commit all changes at the end of every task
- Commit title format: `[spec/fix/feature/refactoring] task name`
- Commit message: Include detailed task summary
- Always sign commits

## Releases

- Stable bare SemVer tags such as `4.4.3` are shared by the Terraform module,
  Elevator CLI, Homebrew cask, and Lambda images. Do not use `elevator-v*`, a
  leading `v`, or prerelease tags.
- Prepare releases on `main` and keep `vars.tf`'s `ecr_repo_tag`, generated
  README documentation, and the intended tag on the same version.
- Pushing the tag is the only release trigger. GitHub Actions creates the
  release and generated notes; do not create a GitHub Release manually.
- Never create, move, delete, or push a release tag without the user's explicit
  authorization. Agents may prepare and verify a release, but the maintainer
  owns the publication trigger.
- macOS releases must remain signed and notarized by Apple Team ID
  `T962D4K3Y7`. Never make signing conditional or allow missing secrets to
  degrade into an unsigned release.
