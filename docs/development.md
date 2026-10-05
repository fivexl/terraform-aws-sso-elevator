# Development

## Python tests

Run from the repository root:

```bash
bash run-tests.sh
```

It syncs the `src/` environment with `uv` (dev extras), runs `pytest` from `src/`, then runs `pre-commit run -a` (ruff, codespell, `uv export` of both `requirements.txt` files, terraform fmt/validate/tflint/docs, trivy). CI runs the same script, so a clean local run means a clean CI run. Pre-commit needs `terraform`, `tflint`, `terraform-docs` (the version CI installs in `.github/workflows/base.yml`) and `trivy` on `PATH`.

The first argument is passed to `pytest`, relative to `src/`:

```bash
bash run-tests.sh tests/test_cache.py
bash run-tests.sh tests/test_cache.py::TestGetCachedAccounts
```

Tests mock AWS and Slack. New code should cover failure paths as well as the happy path: most bugs here are about what happens when AWS, S3 or Slack fails partway through a request.

## CLI tests

The Go CLI is a separate module in `cmd/elevator`:

```bash
(cd cmd/elevator && go vet ./... && go test -race ./...)
sh cmd/elevator/install_test.sh
```

The golden vector in `tests/fixtures/sts_proof/` keeps the Go and Python halves of the CLI identity proof in step.
