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
  case "$1" in
    elevator-v[0-9]*.[0-9]*.[0-9]*) return 0 ;;
    *) die "invalid version format: $1 (expected elevator-vX.Y.Z)" ;;
  esac
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

  fields=$(printf '%s' "$response" \
    | grep -E '"tag_name"|"prerelease"' \
    | sed -E 's/^[[:space:]]*"tag_name": *"([^"]*)".*/TAG \1/; s/^[[:space:]]*"prerelease": *(true|false).*/PRE \1/')

  version=""
  tag=""
  while IFS= read -r line; do
    case "$line" in
      "TAG "*) tag="${line#TAG }" ;;
      "PRE "*)
        pre="${line#PRE }"
        case "$tag" in
          elevator-v*)
            if [ "$pre" = "false" ]; then
              version="$tag"
              break
            fi
            ;;
        esac
        tag=""
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
