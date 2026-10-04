"""Release-time dependency soak gate; the policy is in AGENTS.md "Dependency updates".

report (default): open security alerts, routine Dependabot PRs and manual pins, each with
the newest version that has soaked. verify: every version changed since the last release
tag must have soaked. Stdlib and `gh` only, so running the gate installs nothing unsoaked.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEVERITIES = ["low", "medium", "high", "critical"]
SOAK = timedelta(days=14)
URGENT_SOAK = timedelta(days=7)
UV_DIRS = ["src", "layer"]
GO_DIR = "cmd/elevator"
# Commit-message prefixes from .github/dependabot.yml; labels may not exist in the repo.
ROUTINE_PREFIX = re.compile(r"^(github-actions|terraform|docker|pre-commit):")
ALERT_KINDS = {"pip": "pypi", "go": "go", "actions": "action"}


# --- pure helpers -------------------------------------------------------------


def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version.split("+")[0]))


def is_stable(version: str) -> bool:
    return re.fullmatch(r"v?\d+(\.\d+)*", version) is not None


def normalize(kind: str, name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if kind == "pypi" else name


def ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def day(moment: datetime) -> str:
    return moment.date().isoformat()


def is_urgent(severity: str) -> bool:
    return severity in ("high", "critical")


def soak_for(severity: str) -> timedelta:
    return URGENT_SOAK if is_urgent(severity) else SOAK


def group_alerts(alerts: list[dict]) -> list[dict]:
    """One row per (kind, package): highest severity, highest first-patched version."""
    groups = {}
    for alert in alerts:
        dep = alert["dependency"]
        kind = ALERT_KINDS.get(dep["package"]["ecosystem"], dep["package"]["ecosystem"])
        name = normalize(kind, dep["package"]["name"])
        row = groups.setdefault(
            (kind, name),
            {
                "kind": kind,
                "name": name,
                "severity": "low",
                "required": None,
                "dirs": set(),
                "scopes": set(),
            },
        )
        severity = alert["security_advisory"]["severity"]
        if SEVERITIES.index(severity) > SEVERITIES.index(row["severity"]):
            row["severity"] = severity
        fix = (alert["security_vulnerability"].get("first_patched_version") or {}).get(
            "identifier"
        )
        if fix and (row["required"] is None or vkey(fix) > vkey(row["required"])):
            row["required"] = fix
        row["dirs"].add(str(Path(dep["manifest_path"]).parent))
        row["scopes"].add(dep.get("scope") or "runtime")
    return sorted(
        groups.values(), key=lambda r: (-SEVERITIES.index(r["severity"]), r["name"])
    )


def newest_soaked(
    releases: dict[str, datetime],
    now: datetime,
    soak: timedelta,
    floor: str | None = None,
) -> str | None:
    ok = [
        v
        for v, published in releases.items()
        if now - published >= soak and (floor is None or vkey(v) >= vkey(floor))
    ]
    return max(ok, key=vkey, default=None)


def pick(
    releases: dict[str, datetime],
    current: str | None,
    required: str | None,
    severity: str,
    now: datetime,
) -> tuple[str, str | None]:
    """Status and target version for one security row; `releases` are the versions newer than `current`."""
    if required is None:
        return "NO FIX", None
    if current and vkey(current) >= vkey(required):
        return "APPLIED", current
    target = newest_soaked(releases, now, soak_for(severity), floor=required)
    if target:
        return "READY", target
    newest = newest_soaked(releases, now, soak_for(severity)) or current or "none"
    return f"BLOCKED (fix needs {required}, newest soaked is {newest})", None


def verdict(
    published: datetime, now: datetime, security_fix: bool, override: bool
) -> str:
    if override:
        return "OVERRIDE"
    if now - published >= SOAK:
        return "OK"
    if security_fix and now - published >= URGENT_SOAK:
        return "OK (critical/high fix)"
    return "TOO YOUNG"


def uv_versions(lock_text: str) -> dict[str, str]:
    packages = tomllib.loads(lock_text).get("package", [])
    return {
        normalize("pypi", p["name"]): p["version"]
        for p in packages
        if "registry" in p.get("source", {})
    }


def parse_pins(files: dict[str, str]) -> dict[tuple[str, str, str], set[str]]:
    """Every pin outside uv.lock/go.mod as {(kind, name, where): versions}."""
    pins = defaultdict(set)
    for path, text in files.items():
        if path.startswith(".github/workflows/"):
            for repo, sha in re.findall(
                r"uses:\s*([\w.-]+/[\w.-]+)[^@\s]*@([0-9a-f]{40})", text
            ):
                pins[("action", repo, "workflows")].add(sha)
        elif path == ".pre-commit-config.yaml":
            for repo, rev in re.findall(
                r"repo:\s*https://github\.com/(\S+?)/?\s*\n\s*rev:\s*(\S+)", text
            ):
                pins[("pre-commit", repo, path)].add(rev)
        elif "/docker/Dockerfile" in path:
            for image, digest in re.findall(
                r"^FROM\s+([^\s:@]+)(?::\S+)?@(sha256:[0-9a-f]{64})", text, re.M
            ):
                pins[("docker", image, "src/docker")].add(digest)
        elif path.endswith(".tf"):
            for source, version in re.findall(
                r'source\s*=\s*"([\w-]+/[\w-]+/[\w-]+)(?://\S*)?"\s*\n\s*version\s*=\s*"([\d.]+)"',
                text,
            ):
                pins[("terraform", source, "*.tf")].add(version)
    return pins


def diff_pins(base: dict, head: dict) -> list[tuple[tuple[str, str, str], str, str]]:
    """(key, old versions, new version) for every version present at head but not at base."""
    return [
        (key, ", ".join(sorted(base.get(key, set()))) or "new", version)
        for key in sorted(head)
        for version in sorted(head[key] - base.get(key, set()))
    ]


def go_repo(module: str) -> tuple[str, str]:
    """GitHub repo and tag prefix of a Go module (github.com/aws/aws-sdk-go-v2/config -> config/)."""
    m = re.fullmatch(
        r"(?:github\.com/([^/]+/[^/]+)|golang\.org/x/([^/]+))(?:/(.+))?", module
    )
    if not m:
        raise LookupError(f"no GitHub repo known for {module}")
    sub = re.sub(r"(^|/)v\d+$", "", m[3] or "")
    return m[1] or f"golang/{m[2]}", f"{sub}/" if sub else ""


# --- lookups (network, gh, git, go, docker) -----------------------------------


def run(*cmd: str, cwd: Path = ROOT, env: dict | None = None) -> str:
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout


def gh_json(path: str, *flags: str):  # noqa: ANN201
    return json.loads(run("gh", "api", *flags, path))


def gh_pages(path: str) -> list[dict]:
    return [item for page in gh_json(path, "--paginate", "--slurp") for item in page]


def http(url: str) -> bytes:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "deps_ready"}), timeout=30
    ) as resp:
        return resp.read()


def github_date(repo: str, tag: str) -> datetime:
    """Release time when a release exists (server-set); else the tagger date; else the tagged commit's date."""
    try:
        return ts(gh_json(f"repos/{repo}/releases/tags/{tag}")["published_at"])
    except subprocess.CalledProcessError as e:
        if "HTTP 404" not in e.stderr:
            raise
    obj = gh_json(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    if obj["type"] == "tag":
        return ts(gh_json(f"repos/{repo}/git/tags/{obj['sha']}")["tagger"]["date"])
    return ts(
        gh_json(f"repos/{repo}/commits/{obj['sha']}")["commit"]["committer"]["date"]
    )


def tag_for(repo: str, ref: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    tags = [
        t["name"]
        for t in gh_pages(f"repos/{repo}/tags?per_page=100")
        if t["commit"]["sha"] == ref
    ]
    if not tags:
        raise LookupError(f"no tag on {repo} points at {ref[:12]}")
    return max(tags, key=lambda t: (is_stable(t), len(vkey(t)), vkey(t)))


def pypi_releases(name: str) -> dict[str, datetime]:
    data = json.loads(http(f"https://pypi.org/pypi/{name}/json"))["releases"]
    return {
        v: min(ts(f["upload_time_iso_8601"]) for f in files)
        for v, files in data.items()
        if is_stable(v) and files and not all(f["yanked"] for f in files)
    }


def go_releases(module: str, floor: str) -> dict[str, datetime]:
    repo, prefix = go_repo(module)
    escaped = re.sub("[A-Z]", lambda m: "!" + m[0].lower(), module)
    listed = http(f"https://proxy.golang.org/{escaped}/@v/list").decode().split()
    return {
        v: github_date(repo, prefix + v)
        for v in listed
        if is_stable(v) and vkey(v) > vkey(floor)
    }


def github_releases(repo: str) -> dict[str, datetime]:
    return {
        r["tag_name"]: ts(r["published_at"])
        for r in gh_pages(f"repos/{repo}/releases?per_page=100")
        if not r["draft"] and is_stable(r["tag_name"])
    }


def releases_after(kind: str, name: str, floor: str | None) -> dict[str, datetime]:
    if kind == "go":
        return go_releases(name, floor or "v0")
    if kind == "pypi":
        found = pypi_releases(name)
    elif kind == "action":
        found = github_releases("/".join(name.split("/")[:2]))
    else:
        raise LookupError(f"no release lookup for ecosystem {kind}")
    return {v: d for v, d in found.items() if floor is None or vkey(v) > vkey(floor)}


def published(kind: str, name: str, version: str) -> datetime:
    if kind == "pypi":
        files = json.loads(http(f"https://pypi.org/pypi/{name}/{version}/json"))["urls"]
        return min(ts(f["upload_time_iso_8601"]) for f in files)
    if kind == "go":
        repo, prefix = go_repo(name)
        return github_date(repo, prefix + version)
    if kind == "go-toolchain":
        return github_date("golang/go", version)
    if kind in ("action", "pre-commit"):
        return github_date(name, tag_for(name, version))
    if kind == "terraform":
        return ts(
            json.loads(
                http(f"https://registry.terraform.io/v1/modules/{name}/{version}")
            )["published_at"]
        )
    if kind == "docker":
        image = json.loads(
            run(
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                f"{name}@{version}",
                "--format",
                "{{json .Image}}",
            )
        )
        return max(
            ts(cfg["created"])
            for cfg in ([image] if "created" in image else image.values())
        )
    raise LookupError(f"no publish-date lookup for {kind}")


def read_tree(rev: str | None, pattern: str) -> dict[str, str]:
    """Files matching `pattern` at git `rev`, or in the working tree when rev is None."""
    if rev is None:
        listed = run(
            "git", "ls-files", "--cached", "--others", "--exclude-standard"
        ).splitlines()
        return {
            p: (ROOT / p).read_text()
            for p in fnmatch.filter(listed, pattern)
            if (ROOT / p).is_file()
        }
    listed = run("git", "ls-tree", "-r", "--name-only", rev).splitlines()
    return {
        p: run("git", "show", f"{rev}:{p}") for p in fnmatch.filter(listed, pattern)
    }


def read_file(rev: str | None, path: str) -> str:
    """One file at `rev` (None = working tree); empty when it does not exist there."""
    return read_tree(rev, path).get(path, "")


def go_versions(rev: str | None) -> dict[str, str]:
    """`go list -m all` for cmd/elevator at `rev`, run on a copy so nothing in the checkout changes."""
    if not read_file(rev, f"{GO_DIR}/go.mod"):
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("go.mod", "go.sum"):
            (Path(tmp) / name).write_text(read_file(rev, f"{GO_DIR}/{name}"))
        out = run(
            "go",
            "list",
            "-m",
            "all",
            cwd=Path(tmp),
            env={**os.environ, "GOTOOLCHAIN": "local"},
        )
    return dict(line.split()[:2] for line in out.splitlines()[1:])


def toolchain(rev: str | None) -> str | None:
    found = re.search(r"^toolchain (go\S+)", read_file(rev, f"{GO_DIR}/go.mod"), re.M)
    return found and found[1]


def open_alerts() -> list[dict]:
    return gh_pages("repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=100")


# --- report -------------------------------------------------------------------


def current_version(row: dict) -> str | None:
    if row["kind"] == "pypi":
        found = [
            uv_versions((ROOT / d / "uv.lock").read_text()).get(row["name"])
            for d in row["dirs"]
            if d in UV_DIRS
        ]
    elif row["kind"] == "go":
        found = [go_versions(None).get(row["name"])]
    else:
        found = []
    found = [v for v in found if v]
    return min(found, key=vkey) if found else None


def scope(row: dict) -> str:
    # Dependabot labels optional (dev) extras "runtime"; what the Lambda ships is the --no-dev export.
    if row["kind"] == "pypi":
        exported = "".join(
            (ROOT / d / "requirements.txt").read_text().lower()
            for d in row["dirs"]
            if d in UV_DIRS
        )
        return (
            "runtime"
            if re.search(rf"^{re.escape(row['name'])}==", exported, re.M)
            else "dev"
        )
    return "dev" if row["scopes"] == {"development"} else "runtime"


def newer_line(releases: dict[str, datetime]) -> str:
    listed = ", ".join(
        f"{v} {day(d)}" for v, d in sorted(releases.items(), key=lambda i: vkey(i[0]))
    )
    return f"    newer: {listed}" if listed else ""


def security_rows(now: datetime) -> tuple[list[str], bool, bool]:
    lines, unknown, blocking = [], False, False
    try:
        rows = group_alerts(open_alerts())
    except Exception as e:  # noqa: BLE001 -- any failure must surface as UNKNOWN, not a clean report
        return [f"UNKNOWN  could not read Dependabot alerts: {e}"], True, False
    for row in rows:
        where = ", ".join(sorted(row["dirs"]))
        head = f"{row['name']}  {row['severity']}"
        try:
            current = current_version(row)
            releases = releases_after(row["kind"], row["name"], current)
            status, target = pick(
                releases, current, row["required"], row["severity"], now
            )
        except Exception as e:  # noqa: BLE001
            lines.append(f"{head}  UNKNOWN ({e})  ({where})")
            unknown = True
            continue
        shown = target or row["required"]
        when = releases.get(shown)
        date = f"published {day(when)}" if when else "published ?"
        if status.startswith("BLOCKED") and when:
            date += f", ready {day(when + soak_for(row['severity']))}"
        lines.append(
            f"{row['name']}  {current or '?'} -> {shown or '-'}  {row['severity']}  {scope(row)}  {date}  {status}  ({where})"
        )
        lines.append(newer_line(releases))
        if status == "READY" and is_urgent(row["severity"]):
            blocking = True
    return [line for line in lines if line], unknown, blocking


def routine_rows() -> tuple[list[str], bool]:
    try:
        prs = json.loads(
            run(
                "gh",
                "pr",
                "list",
                "--author",
                "app/dependabot",
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "number,title",
            )
        )
    except Exception as e:  # noqa: BLE001
        return [f"UNKNOWN  could not list Dependabot PRs: {e}"], True
    return [
        f"#{pr['number']}  {pr['title']}"
        for pr in prs
        if ROUTINE_PREFIX.match(pr["title"])
    ], False


def manual_rows(now: datetime) -> tuple[list[str], bool]:
    lines, unknown = [], False
    workflow = (ROOT / ".github/workflows/cli-release.yml").read_text()
    items = [
        ("go toolchain", lambda: toolchain(None), go_toolchains),
        (
            "syft",
            lambda: re.search(r'syft-version:\s*"([^"]+)"', workflow)[1],
            lambda: github_releases("anchore/syft"),
        ),
        (
            "goreleaser",
            lambda: re.search(
                r'goreleaser-action@.*\n\s*with:\s*\n\s*version:\s*"([^"]+)"', workflow
            )[1],
            lambda: github_releases("goreleaser/goreleaser"),
        ),
    ]
    for label, get_current, get_releases in items:
        try:
            current = get_current()
            releases = {
                v: d for v, d in get_releases().items() if vkey(v) > vkey(current)
            }
        except Exception as e:  # noqa: BLE001
            lines.append(f"{label}  UNKNOWN ({e})")
            unknown = True
            continue
        target = newest_soaked(releases, now, SOAK)
        if target:
            lines.append(
                f"{label}  {current} -> {target}  published {day(releases[target])}  READY"
            )
        else:
            lines.append(f"{label}  {current}  UP TO DATE")
        lines.append(newer_line(releases))
    return [line for line in lines if line], unknown


def go_toolchains() -> dict[str, datetime]:
    """Stable patch releases in the go.mod toolchain's minor line, dated by their golang/go tags."""
    line = ".".join(toolchain(None).split(".")[:2])
    listed = json.loads(http("https://go.dev/dl/?mode=json&include=all"))
    return {
        r["version"]: github_date("golang/go", r["version"])
        for r in listed
        if r["stable"] and r["version"].startswith(f"{line}.")
    }


def report(now: datetime) -> int:
    security, unknown_s, blocking = security_rows(now)
    routine, unknown_r = routine_rows()
    manual, unknown_m = manual_rows(now)
    for title, lines in (
        ("Security (open Dependabot alerts)", security),
        ("Routine (open Dependabot PRs)", routine),
        ("Manual pins", manual),
    ):
        print(f"== {title}")
        print("\n".join(lines) or "none")
        print()
    if unknown_s or unknown_r or unknown_m:
        print("FAIL: some lookups returned UNKNOWN; the report is incomplete.")
    if blocking:
        print(
            "FAIL: a READY critical/high fix is not applied yet; apply it before tagging."
        )
    return 1 if unknown_s or unknown_r or unknown_m or blocking else 0


# --- verify -------------------------------------------------------------------


def snapshot(rev: str | None) -> dict[tuple[str, str, str], set[str]]:
    files = {}
    for pattern in (
        ".github/workflows/*.yml",
        ".pre-commit-config.yaml",
        "src/docker/Dockerfile*",
        "*.tf",
    ):
        files.update(read_tree(rev, pattern))
    pins = parse_pins(files)
    for d in UV_DIRS:
        for name, version in uv_versions(read_file(rev, f"{d}/uv.lock")).items():
            pins[("pypi", name, d)].add(version)
    for module, version in go_versions(rev).items():
        pins[("go", module, GO_DIR)].add(version)
    if go_toolchain := toolchain(rev):
        pins[("go-toolchain", "go", GO_DIR)].add(go_toolchain)
    return pins


def overrides() -> set[tuple[str, str]]:
    found = set()
    for d in UV_DIRS:
        entries = (
            tomllib.loads((ROOT / d / "pyproject.toml").read_text())
            .get("tool", {})
            .get("uv", {})
            .get("exclude-newer-package", {})
        )
        found |= {(normalize("pypi", name), d) for name in entries}
    return found


def urgent_fixes(alerts: list[dict]) -> dict[tuple[str, str], str]:
    return {
        (r["kind"], r["name"]): r["required"]
        for r in group_alerts(alerts)
        if is_urgent(r["severity"]) and r["required"]
    }


def short(version: str) -> str:
    return (
        version[:19]
        if version.startswith("sha256:")
        else version[:12]
        if re.fullmatch(r"[0-9a-f]{40}", version)
        else version
    )


def verify(base: str, now: datetime) -> int:
    failed = False
    try:
        fixes = urgent_fixes(open_alerts())
    except Exception as e:  # noqa: BLE001
        print(
            f"UNKNOWN  could not read Dependabot alerts, no 7-day exceptions applied: {e}"
        )
        fixes, failed = {}, True
    overridden = overrides()
    changes = diff_pins(snapshot(base), snapshot(None))
    print(f"== Versions changed since {base}")
    for (kind, name, where), old, new in changes:
        head = f"{kind} {name} ({where})  {short(old)} -> {short(new)}"
        try:
            date = published(kind, name, new)
        except Exception as e:  # noqa: BLE001
            print(f"{head}  UNKNOWN ({e})")
            failed = True
            continue
        required = fixes.get((kind, name))
        status = verdict(
            date,
            now,
            bool(required) and vkey(new) >= vkey(required),
            (name, where) in overridden,
        )
        print(f"{head}  published {day(date)}  {status}")
        failed |= status == "TOO YOUNG"
    if not changes:
        print("none")
    return 1 if failed else 0


def latest_tag() -> str:
    return run(
        "git", "describe", "--tags", "--abbrev=0", "--match", "[0-9]*.[0-9]*.[0-9]*"
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("report")
    sub.add_parser("verify").add_argument(
        "--base", help="git ref to compare against (default: latest SemVer tag)"
    )
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.command == "verify":
        return verify(args.base or latest_tag(), now)
    return report(now)


if __name__ == "__main__":
    sys.exit(main())
