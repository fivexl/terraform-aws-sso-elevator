# Claude Code Instructions

## Before Committing

Always run these commands before committing changes:

```bash
# Fix linting errors and format code
cd src && uv run ruff check --fix . && uv run ruff format .

# Update terraform documentation
cd /Users/tom/Documents/projects/terraform-aws-sso-elevator && terraform-docs markdown table --output-file README.md --output-mode inject .
```

## Running Tests

```bash
cd src && uv run pytest -q
```
