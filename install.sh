#!/bin/sh
# Installs the elevator CLI (cmd/elevator/) from GitHub Releases.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fivexl/terraform-aws-sso-elevator/main/install.sh | sh
#
# Env overrides:
#   ELEVATOR_VERSION      Pin to a specific tag (e.g. 4.4.3) instead of latest.
#   ELEVATOR_INSTALL_DIR  Install directory (default: $HOME/.local/bin).
#   GITHUB_TOKEN          Used to fetch releases at the authenticated 5000/hr rate
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

# Only stable bare SemVer tags are releaseable. Keep the installer on exactly
# the same contract so a value from an untrusted environment cannot change the
# download path or select an unpublished tag shape.
validate_version() {
  nl='
'
  case "$1" in
    *"$nl"*) die "invalid version format: $1 (must not contain a newline)" ;;
  esac
  echo "$1" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
    || die "invalid version format: $1 (expected stable X.Y.Z)"
}

get_latest_version() {
  endpoint="https://api.github.com/repos/${REPO}/releases/latest"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    response=$(curl -fsSL -H "Authorization: token ${GITHUB_TOKEN}" "$endpoint") \
      || die "failed to fetch the latest release from the GitHub API"
  else
    response=$(curl -fsSL "$endpoint") \
      || die "failed to fetch the latest release from the GitHub API (if this is a rate limit, set GITHUB_TOKEN and retry)"
  fi
  version=$(printf '%s\n' "$response" | sed -n 's/^[[:space:]]*"tag_name": *"\([^"]*\)".*/\1/p' | sed -n '1p')
  [ -n "$version" ] || die "the GitHub API response did not contain a release tag"
  echo "$version"
}

verify_checksum() {
  file="$1"; checksums_file="$2"
  [ -s "$checksums_file" ] || die "checksums.txt is missing or empty — refusing to install unverified"
  # An exact field comparison, not a grep regex (#194 E1): the previous
  # `grep " $(basename "$file")\$"` left "." unescaped, so e.g.
  # "elevator-darwin-arm64.tar.gz" also matched a line for
  # "elevator-darwin-arm64Xtar.gz" -- not exploitable against real
  # GoReleaser output (fixed filenames, and any spurious extra match would
  # just produce a value that fails the comparison below), but this is
  # unambiguous by construction rather than relying on the archive name
  # never containing a regex metacharacter.
  expected=$(awk -v want="$(basename "$file")" '$2 == want { print $1 }' "$checksums_file")
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
# simply being absent, or simply not authenticated -- see below), that's
# treated as a real integrity failure, the same as a checksum mismatch
# above.
#
# --signer-workflow, not just --repo (#194 C3): --repo alone accepts an
# attestation signed by *any* workflow in this repo with id-token/
# attestations: write, not specifically cli-release.yml -- another workflow
# gaining that permission later (or being compromised) could attest
# something as this release without this check noticing. Pinning the exact
# signer workflow path closes that -- only an attestation actually signed
# by cli-release.yml passes.
SIGNER_WORKFLOW="${REPO}/.github/workflows/cli-release.yml"
verify_attestation() {
  file="$1"
  if ! command -v gh >/dev/null 2>&1; then
    log "note: gh CLI not found on PATH — skipping build-provenance attestation verification (only the checksum above was verified)"
    return 0
  fi
  # gh being present doesn't mean it's usable for this: many machines
  # (including GitHub's own hosted runners) ship gh preinstalled but never
  # run `gh auth login`, and gh attestation verify then fails with an
  # authentication prompt -- a tooling condition, not a sign the artifact
  # is untrusted. Checked separately from the verify call itself so that
  # distinction doesn't get lost: an unauthenticated gh degrades to the
  # same "skip and note it" path as gh being absent entirely, rather than
  # refusing an install that was never actually checked.
  if ! gh auth status >/dev/null 2>&1; then
    log "note: gh CLI is not authenticated (run 'gh auth login') — skipping build-provenance attestation verification (only the checksum above was verified)"
    return 0
  fi
  if ! gh attestation verify "$file" --repo "$REPO" --signer-workflow "$SIGNER_WORKFLOW" >/dev/null 2>&1; then
    die "build-provenance attestation verification failed for $(basename "$file") — refusing to install (run 'gh attestation verify $file --repo $REPO --signer-workflow $SIGNER_WORKFLOW' for details)"
  fi
  log "Verified build-provenance attestation for $(basename "$file")"
}

# verify_archive_members whitelists tar entries as regular files only,
# rejecting symlinks (and hardlinks -- #194 E2, GNU tar -tv marks these with
# a leading "h") so extraction can't be tricked into writing outside the
# staging directory. Not exploitable today given extraction only ever
# targets one specific named member (see main()'s `tar -xzf ... "$BINARY_NAME"`
# below) into a directory `-C` already confines it to, and both GNU and BSD
# tar refuse ".." path segments in members by default regardless -- this is
# hardening for what this function's own doc comment already claims to do,
# not a response to a live gap.
verify_archive_members() {
  archive="$1"
  tar -tvf "$archive" | while IFS= read -r line; do
    case "$line" in
      l*) die "archive contains a symlink entry, refusing to extract: $line" ;;
      h*) die "archive contains a hardlink entry, refusing to extract: $line" ;;
    esac
  done
}

verify_macos_signature() {
  os="$1"
  file="$2"
  [ "$os" = darwin ] || return 0
  command -v codesign >/dev/null 2>&1 || die "codesign is required to verify the macOS release"
  requirement='=anchor apple generic and certificate leaf[subject.OU] = T962D4K3Y7'
  codesign --verify --strict --verbose=2 "$file" \
    || die "invalid Apple code signature for $(basename "$file") — refusing to install"
  codesign --verify -R "$requirement" "$file" \
    || die "$(basename "$file") is not signed by expected Apple Team ID T962D4K3Y7 — refusing to install"
  log "Verified Apple code signature for Team ID T962D4K3Y7"
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
  if ! work_dir=$(mktemp -d "${INSTALL_DIR}/.${BINARY_NAME}-install.XXXXXX" 2>/dev/null); then
    # This fallback silently reintroduced exactly the non-atomic-install
    # risk the comment above exists to prevent (#194 E4): a work_dir outside
    # INSTALL_DIR's filesystem makes the mv below a cross-device copy
    # (POSIX mv falls back to copy+unlink when rename(2) returns EXDEV, not
    # a single atomic rename), so an install interrupted mid-copy can leave
    # a truncated binary at the destination -- with nothing here saying
    # that weaker path was actually taken.
    log "warning: could not create a temp directory inside ${INSTALL_DIR} (unwritable, or on a filesystem that doesn't support this template) — falling back to the system temp directory, which makes the final install step a cross-device copy instead of an atomic rename"
    work_dir=$(mktemp -d)
  fi
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
  verify_macos_signature "$os" "${work_dir}/${BINARY_NAME}"
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
