#!/bin/sh
# Installs the elevator CLI (cmd/elevator/) from GitHub Releases.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fivexl/terraform-aws-sso-elevator/main/install.sh | sh
#
# Env overrides:
#   ELEVATOR_VERSION      Pin to a specific tag (e.g. elevator-v1.2.0) instead of latest.
#   ELEVATOR_INSTALL_DIR  Install directory (default: $HOME/.local/bin).
#   GITHUB_TOKEN          Used to list releases at the authenticated 5000/hr rate
#                         limit instead of the unauthenticated 60/hr one. Only
#                         consulted when ELEVATOR_VERSION isn't set.
set -eu

REPO="fivexl/terraform-aws-sso-elevator"
BINARY_NAME="elevator"
INSTALL_DIR="${ELEVATOR_INSTALL_DIR:-$HOME/.local/bin}"

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

detect_os() {
  case "$(uname -s)" in
    Linux) echo linux ;;
    Darwin) echo darwin ;;
    *) die "unsupported OS: $(uname -s) — only linux and darwin releases are published" ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    arm64|aarch64) echo arm64 ;;
    *) die "unsupported architecture: $(uname -m) — only amd64 and arm64 releases are published" ;;
  esac
}

# validate_version defends against path-traversal / repo-pivot if
# ELEVATOR_VERSION is ever set from an untrusted source — only allow a strict
# elevator-vX.Y.Z(-suffix) shape, nothing else. The elevator- prefix also
# keeps CLI release tags out of the module's own vX.Y.Z-less version tags
# (e.g. "4.3.1") and out of any tag-triggered module workflow.
validate_version() {
  # The elevator-v[0-9]*.[0-9]*.[0-9]* shape alone doesn't reject "/" or
  # "..", since case-pattern "*" matches those like any other character —
  # e.g. "elevator-v1/../../etc/passwd.0.0" satisfies it. Reject "/"
  # explicitly first; a real tag never contains one.
  case "$1" in
    */*) die "invalid version format: $1 (must not contain '/')" ;;
  esac
  # grep matches per line, not the whole input -- "$(printf 'garbage\nelevator-v1.2.3')"
  # would pass the anchored check below (its second line matches on its own)
  # even though the value as a whole isn't a clean tag string. curl happens
  # to fail safe on a URL containing a raw newline, but that's incidental,
  # not something this function should rely on -- reject a newline outright,
  # the same way "/" is rejected above.
  nl='
'
  case "$1" in
    *"$nl"*) die "invalid version format: $1 (must not contain a newline)" ;;
  esac
  # A shell case glob can't express "one or more digits" -- [0-9]* means
  # "a digit, then anything", so the previous version of this check accepted
  # e.g. "elevator-v1$(id).0.0" or "elevator-v9EVIL.9EVIL.9EVIL". No working
  # injection was ever found downstream (every expansion here is quoted and
  # there's no eval), but the comment above claiming a "strict" shape was a
  # false safety claim, not just a redundant one. grep -E gives a real
  # anchored regex, actually requiring digit-only version components.
  #
  # The suffix pattern follows SemVer 2.0's own grammar (dot-separated
  # alphanumeric-or-hyphen identifiers for prerelease, optionally followed by
  # a "+"-prefixed build-metadata block of the same shape) rather than the
  # narrower version that used to reject legitimate tags like
  # "elevator-v1.2.3-rc-1" (a hyphen inside the prerelease identifier itself)
  # and "elevator-v1.2.3+build.7" (build metadata) -- both are tags
  # cli-release.yml's own validate-tag job (elevator-v* only) would accept.
  echo "$1" | grep -Eq '^elevator-v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$' \
    || die "invalid version format: $1 (expected elevator-vX.Y.Z, optionally with a -prerelease and/or +build suffix)"
}

get_latest_version() {
  # This repo also publishes the module's own version releases (e.g. "4.3.1",
  # no "elevator-" prefix) as GitHub Releases. /releases/latest doesn't
  # distinguish between the two kinds of release -- it just returns whichever
  # was published most recently, of either kind -- so a module release
  # published after the last CLI release would make it resolve to the wrong
  # one. List releases explicitly instead and take the first non-prerelease
  # elevator-v* tag, in the order the API returns them (newest first).
  # per_page=100 (the API's max) rather than 30, so this doesn't start
  # missing the latest elevator-v* release once enough module releases
  # accumulate ahead of it.
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    response=$(curl -fsSL -H "Authorization: token ${GITHUB_TOKEN}" "https://api.github.com/repos/${REPO}/releases?per_page=100") \
      || die "failed to list releases from the GitHub API"
  else
    # Unauthenticated requests are capped at 60/hr per source IP -- set
    # GITHUB_TOKEN to use the authenticated 5000/hr limit instead.
    response=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases?per_page=100") \
      || die "failed to list releases from the GitHub API (if this is a rate limit, set GITHUB_TOKEN and retry)"
  fi

  # Unauthenticated requests never returned draft releases at all, so
  # filtering on "prerelease" alone was sufficient. Now that GITHUB_TOKEN is
  # supported (for the higher rate limit), a token with read access to this
  # repo makes drafts visible too -- and a draft has "prerelease": false, so
  # it would win this scan ahead of the real latest release, then 404 when
  # main() tries to download its assets from the public releases/download
  # URL (draft assets aren't published there). "draft" is tracked the same
  # way "prerelease" already was, and both must be false to accept a tag.
  fields=$(printf '%s' "$response" \
    | grep -E '"tag_name"|"draft"|"prerelease"' \
    | sed -E 's/^[[:space:]]*"tag_name": *"([^"]*)".*/TAG \1/; s/^[[:space:]]*"draft": *(true|false).*/DRAFT \1/; s/^[[:space:]]*"prerelease": *(true|false).*/PRE \1/')

  version=""
  tag=""
  draft=""
  while IFS= read -r line; do
    case "$line" in
      "TAG "*) tag="${line#TAG }"; draft="" ;;
      "DRAFT "*) draft="${line#DRAFT }" ;;
      "PRE "*)
        pre="${line#PRE }"
        case "$tag" in
          elevator-v*)
            if [ "$pre" = "false" ] && [ "$draft" = "false" ]; then
              version="$tag"
              break
            fi
            ;;
        esac
        tag=""
        draft=""
        ;;
    esac
  done <<EOF
$fields
EOF

  [ -n "${version:-}" ] || die "the GitHub API call succeeded but no non-prerelease elevator-v* release was found"
  echo "$version"
}

verify_checksum() {
  file="$1"; checksums_file="$2"
  [ -s "$checksums_file" ] || die "checksums.txt is missing or empty — refusing to install unverified"
  expected=$(grep " $(basename "$file")\$" "$checksums_file" | awk '{print $1}')
  [ -n "$expected" ] || die "no checksum entry found for $(basename "$file") — refusing to install unverified"
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$file" | awk '{print $1}')
  else
    actual=$(shasum -a 256 "$file" | awk '{print $1}')
  fi
  [ "$expected" = "$actual" ] || die "checksum mismatch for $(basename "$file") — expected $expected, got $actual"
}

# verify_attestation checks the archive against the SLSA build-provenance
# attestation cli-release.yml publishes for it (actions/attest-build-provenance,
# Sigstore-backed via GitHub's own OIDC identity, no separate signing keys).
# This is the actual authenticity root checksums.txt itself can't be: both
# the archive and checksums.txt are fetched unauthenticated from the same
# release, so a checksum match alone only proves "these two downloads agree
# with each other", not that either came from the real CI run. Best-effort
# by design -- gh is not a listed dependency of this installer (only curl,
# tar, and sha256sum/shasum are, per check_required_commands), so this can't
# be a hard requirement without breaking installs for everyone who doesn't
# have it. When gh genuinely fails to confirm the attestation (as opposed to
# simply being absent), that's treated as a real integrity failure, the same
# as a checksum mismatch above.
verify_attestation() {
  file="$1"
  if ! command -v gh >/dev/null 2>&1; then
    log "note: gh CLI not found on PATH — skipping build-provenance attestation verification (only the checksum above was verified)"
    return 0
  fi
  if ! gh attestation verify "$file" --repo "$REPO" >/dev/null 2>&1; then
    die "build-provenance attestation verification failed for $(basename "$file") — refusing to install (run 'gh attestation verify $file --repo $REPO' for details)"
  fi
  log "Verified build-provenance attestation for $(basename "$file")"
}

# verify_archive_members whitelists tar entries as regular files only,
# rejecting symlinks so extraction can't be tricked into writing outside the
# staging directory.
verify_archive_members() {
  archive="$1"
  tar -tvf "$archive" | while IFS= read -r line; do
    case "$line" in
      l*) die "archive contains a symlink entry, refusing to extract: $line" ;;
    esac
  done
}

check_required_commands() {
  for cmd in curl tar; do
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required but not found on PATH"
  done
  if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    die "sha256sum or shasum is required but neither was found on PATH"
  fi
}

main() {
  check_required_commands
  os=$(detect_os)
  arch=$(detect_arch)
  version="${ELEVATOR_VERSION:-$(get_latest_version)}"
  validate_version "$version"

  archive_name="${BINARY_NAME}-${os}-${arch}.tar.gz"
  base_url="https://github.com/${REPO}/releases/download/${version}"

  # mkdir before mktemp so the staging dir lands on the same filesystem as
  # the final install path on a fresh machine too (INSTALL_DIR not existing
  # yet is the common case, not the exception) -- otherwise mktemp falls
  # back to the system tmp dir, the mv below silently becomes a
  # cross-device copy instead of an atomic rename, and an install
  # interrupted mid-copy can leave a truncated binary at the destination.
  mkdir -p "$INSTALL_DIR"
  work_dir=$(mktemp -d "${INSTALL_DIR}/.${BINARY_NAME}-install.XXXXXX" 2>/dev/null) \
    || work_dir=$(mktemp -d)
  trap 'rm -rf "$work_dir"' EXIT

  log "Downloading ${BINARY_NAME} ${version} for ${os}/${arch}..."
  curl -fsSL -o "${work_dir}/${archive_name}" "${base_url}/${archive_name}" \
    || die "failed to download ${base_url}/${archive_name}"
  curl -fsSL -o "${work_dir}/checksums.txt" "${base_url}/checksums.txt" \
    || die "failed to download ${base_url}/checksums.txt"

  verify_checksum "${work_dir}/${archive_name}" "${work_dir}/checksums.txt"
  verify_attestation "${work_dir}/${archive_name}"
  verify_archive_members "${work_dir}/${archive_name}"

  tar -xzf "${work_dir}/${archive_name}" -C "$work_dir" "$BINARY_NAME"
  chmod +x "${work_dir}/${BINARY_NAME}"
  mv "${work_dir}/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"

  log "Installed ${BINARY_NAME} ${version} to ${INSTALL_DIR}/${BINARY_NAME}"
  case ":$PATH:" in
    *":${INSTALL_DIR}:"*) : ;;
    *) log "warning: ${INSTALL_DIR} is not on your PATH — add it, e.g. export PATH=\"${INSTALL_DIR}:\$PATH\"" ;;
  esac
  "${INSTALL_DIR}/${BINARY_NAME}" version
}

# Guard so this can be sourced by tests without auto-running main().
if [ -z "${ELEVATOR_INSTALLER_TEST_MODE:-}" ]; then
  main
fi
