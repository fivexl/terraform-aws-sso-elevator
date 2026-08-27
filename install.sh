#!/bin/sh
# Installs the elevator CLI (cmd/elevator/) from GitHub Releases.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fivexl/terraform-aws-sso-elevator/main/install.sh | sh
#
# Env overrides:
#   ELEVATOR_VERSION      Pin to a specific tag (e.g. elevator-v1.2.0) instead of latest.
#   ELEVATOR_INSTALL_DIR  Install directory (default: $HOME/.local/bin).
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
  # Resolve via the /releases/latest redirect's Location header first — avoids
  # GitHub API rate limits. Fall back to the API if that fails for any reason.
  version=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
    "https://github.com/${REPO}/releases/latest" 2>/dev/null | sed -n 's#.*/tag/##p') || true
  if [ -z "${version:-}" ]; then
    version=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
      | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p')
  fi
  [ -n "${version:-}" ] || die "could not resolve the latest release version"
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

main() {
  os=$(detect_os)
  arch=$(detect_arch)
  version="${ELEVATOR_VERSION:-$(get_latest_version)}"
  validate_version "$version"

  archive_name="${BINARY_NAME}-${os}-${arch}.tar.gz"
  base_url="https://github.com/${REPO}/releases/download/${version}"

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
  mkdir -p "$INSTALL_DIR"
  chmod +x "${work_dir}/${BINARY_NAME}"
  mv "${work_dir}/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"

  cat > "${INSTALL_DIR}/.${BINARY_NAME}.install.json" <<EOF
{"installer": "install.sh", "version": "${version}", "os": "${os}", "arch": "${arch}"}
EOF

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
