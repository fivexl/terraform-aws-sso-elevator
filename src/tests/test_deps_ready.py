"""Unit tests for scripts/deps_ready.py; every gh/network/git lookup is mocked."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location("deps_ready", Path(__file__).resolve().parents[2] / "scripts" / "deps_ready.py")
deps_ready = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deps_ready)

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def days_ago(n):
    return NOW - timedelta(days=n)


def alert(name, severity, fix, manifest="src/uv.lock"):
    return {
        "dependency": {"package": {"ecosystem": "pip", "name": name}, "manifest_path": manifest, "scope": "runtime"},
        "security_advisory": {"severity": severity},
        "security_vulnerability": {"first_patched_version": {"identifier": fix} if fix else None},
    }


def test_vkey_and_stability():
    assert deps_ready.vkey("v1.10.0") > deps_ready.vkey("v1.9.9")
    assert deps_ready.vkey("2.8.0") > deps_ready.vkey("2.7")
    assert deps_ready.is_stable("v2.18.1")
    assert not deps_ready.is_stable("2.8.0rc1")
    assert not deps_ready.is_stable("v2.19.0-61704ffc-nightly")


def test_group_alerts_merges_advisories_per_package():
    rows = deps_ready.group_alerts(
        [
            alert("urllib3", "medium", "2.7.1", manifest="layer/requirements.txt"),
            alert("urllib3", "high", "2.8.0"),
            alert("Pygments", "low", "2.20.0"),
            alert("pygments", "low", None),
        ]
    )
    assert [r["name"] for r in rows] == ["urllib3", "pygments"]
    urllib3 = rows[0]
    assert urllib3["severity"] == "high"
    assert urllib3["required"] == "2.8.0"
    assert urllib3["dirs"] == {"src", "layer"}
    assert rows[1]["required"] == "2.20.0"


def test_group_alerts_without_any_fix():
    (row,) = deps_ready.group_alerts([alert("pkg", "critical", None)])
    assert row["required"] is None


def test_pick_ready_takes_newest_soaked_fix():
    releases = {"2.8.0": days_ago(20), "2.8.1": days_ago(10), "2.9.0": days_ago(3)}
    assert deps_ready.pick(releases, "2.7.0", "2.8.0", "medium", NOW) == ("READY", "2.8.0")
    # critical/high soak 7 days, so 2.8.1 qualifies
    assert deps_ready.pick(releases, "2.7.0", "2.8.0", "high", NOW) == ("READY", "2.8.1")


def test_pick_blocked_names_fix_and_newest_soaked():
    releases = {"2.7.1": days_ago(30), "2.8.0": days_ago(10)}
    status, target = deps_ready.pick(releases, "2.7.0", "2.8.0", "medium", NOW)
    assert status == "BLOCKED (fix needs 2.8.0, newest soaked is 2.7.1)"
    assert target is None


def test_pick_applied_and_no_fix():
    assert deps_ready.pick({}, "2.8.0", "2.8.0", "high", NOW) == ("APPLIED", "2.8.0")
    assert deps_ready.pick({}, "2.7.0", None, "high", NOW) == ("NO FIX", None)


@pytest.mark.parametrize(
    ("age", "security_fix", "override", "expected"),
    [
        (14, False, False, "OK"),
        (13, False, False, "TOO YOUNG"),
        (7, True, False, "OK (critical/high fix)"),
        (6, True, False, "TOO YOUNG"),
        (1, False, True, "OVERRIDE"),
    ],
)
def test_verdict(age, security_fix, override, expected):
    assert deps_ready.verdict(days_ago(age), NOW, security_fix, override) == expected


def test_uv_versions_skips_the_project_itself():
    lock = """
[[package]]
name = "sso-elevator"
version = "4.2.2"
source = { editable = "." }

[[package]]
name = "Typing_Extensions"
version = "4.15.0"
source = { registry = "https://pypi.org/simple" }
"""
    assert deps_ready.uv_versions(lock) == {"typing-extensions": "4.15.0"}


def test_parse_and_diff_pins():
    sha_a, sha_b = "a" * 40, "b" * 40
    digest = "sha256:" + "c" * 64

    def files(sha, tf_version, rev):
        return {
            ".github/workflows/base.yml": (
                f"      - uses: actions/checkout@{sha} # v6\n      - uses: github/codeql-action/upload-sarif@{sha_a}\n"
            ),
            ".pre-commit-config.yaml": f"-   repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: {rev}\n",
            "src/docker/Dockerfile": f"FROM ghcr.io/astral-sh/uv:0.12.17@{digest} AS uv\nFROM scratch\n",
            "layers.tf": f'module "x" {{\n  source  = "terraform-aws-modules/lambda/aws"\n  version = "{tf_version}"\n}}\n',
            "versions.tf": '    aws = {\n      source  = "hashicorp/aws"\n      version = ">= 4.64"\n    }\n',
        }

    base = deps_ready.parse_pins(files(sha_a, "8.1.2", "v0.14.9"))
    head = deps_ready.parse_pins(files(sha_b, "8.9.0", "v0.14.9"))
    assert base[("docker", "ghcr.io/astral-sh/uv", "src/docker")] == {digest}
    assert ("terraform", "hashicorp/aws", "*.tf") not in base
    assert deps_ready.diff_pins(base, head) == [
        (("action", "actions/checkout", "workflows"), sha_a, sha_b),
        (("terraform", "terraform-aws-modules/lambda/aws", "*.tf"), "8.1.2", "8.9.0"),
    ]


def test_diff_pins_reports_new_entries():
    assert deps_ready.diff_pins({}, {("pypi", "idna", "src"): {"3.15"}}) == [(("pypi", "idna", "src"), "new", "3.15")]


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("github.com/aws/aws-sdk-go-v2", ("aws/aws-sdk-go-v2", "")),
        ("github.com/aws/aws-sdk-go-v2/config", ("aws/aws-sdk-go-v2", "config/")),
        ("github.com/aws/aws-sdk-go-v2/internal/endpoints/v2", ("aws/aws-sdk-go-v2", "internal/endpoints/")),
        ("github.com/foo/bar/v3", ("foo/bar", "")),
        ("golang.org/x/net", ("golang/net", "")),
    ],
)
def test_go_repo(module, expected):
    assert deps_ready.go_repo(module) == expected


def test_go_repo_unknown_host():
    with pytest.raises(LookupError):
        deps_ready.go_repo("gopkg.in/yaml.v3")


def test_security_rows_flags_ready_high_fix_and_unknowns():
    alerts = [alert("urllib3", "high", "2.8.0"), alert("idna", "medium", "3.15")]

    def releases_after(_kind, name, _floor):
        if name == "idna":
            raise LookupError("PyPI down")
        return {"2.8.0": days_ago(19)}

    with (
        patch.object(deps_ready, "open_alerts", return_value=alerts),
        patch.object(deps_ready, "current_version", return_value="2.7.0"),
        patch.object(deps_ready, "releases_after", side_effect=releases_after),
        patch.object(deps_ready, "scope", return_value="runtime"),
    ):
        lines, unknown, blocking = deps_ready.security_rows(NOW)
    assert lines[0] == "urllib3  2.7.0 -> 2.8.0  high  runtime  published 2026-09-15  READY  (src)"
    assert any("idna  medium  UNKNOWN (PyPI down)" in line for line in lines)
    assert unknown
    assert blocking


def test_security_rows_alert_lookup_failure_is_unknown():
    with patch.object(deps_ready, "open_alerts", side_effect=RuntimeError("401")):
        lines, unknown, blocking = deps_ready.security_rows(NOW)
    assert unknown
    assert not blocking
    assert "UNKNOWN" in lines[0]


def test_report_exit_code():
    with (
        patch.object(deps_ready, "security_rows", return_value=(["x"], False, False)),
        patch.object(deps_ready, "routine_rows", return_value=([], False)),
        patch.object(deps_ready, "manual_rows", return_value=([], False)),
    ):
        assert deps_ready.report(NOW) == 0
    with (
        patch.object(deps_ready, "security_rows", return_value=(["x"], False, True)),
        patch.object(deps_ready, "routine_rows", return_value=([], False)),
        patch.object(deps_ready, "manual_rows", return_value=([], False)),
    ):
        assert deps_ready.report(NOW) == 1


def test_verify_fails_on_young_version_and_honours_overrides():
    base = {("pypi", "urllib3", "src"): {"2.7.0"}, ("pypi", "idna", "src"): {"3.11"}}
    head = {("pypi", "urllib3", "src"): {"2.8.0"}, ("pypi", "idna", "src"): {"3.20"}}
    dates = {"2.8.0": days_ago(9), "3.20": days_ago(3)}
    with (
        patch.object(deps_ready, "open_alerts", return_value=[alert("urllib3", "high", "2.8.0")]),
        patch.object(deps_ready, "snapshot", side_effect=lambda rev: base if rev else head),
        patch.object(deps_ready, "published", side_effect=lambda _kind, _name, version: dates[version]),
        patch.object(deps_ready, "overrides", return_value=set()),
    ):
        assert deps_ready.verify("4.4.3", NOW) == 1  # idna 3.20 is 3 days old
        with patch.object(deps_ready, "overrides", return_value={("idna", "src")}):
            assert deps_ready.verify("4.4.3", NOW) == 0  # urllib3 passes as a 9-day-old high fix
