#!/bin/sh
set -eu

repo_root=$(CDPATH='' cd -- "$(dirname "$0")/../.." && pwd)
installer="$repo_root/install.sh"

accepts_version() {
  ELEVATOR_INSTALLER_TEST_MODE=1 sh -c '. "$1"; validate_version "$2"' sh "$installer" "$1"
}

rejects_version() {
  if accepts_version "$1" >/dev/null 2>&1; then
    printf 'expected version to be rejected: %s\n' "$1" >&2
    exit 1
  fi
}

accepts_version 4.4.3
accepts_version 10.20.300
rejects_version elevator-v4.4.3
rejects_version v4.4.3
rejects_version 4.4
rejects_version 4.4.3-rc.1
rejects_version 4.4.3+build
rejects_version 4.4.x
rejects_version 04.4.3
rejects_version '../4.4.3'

# Linux must not depend on Apple's tooling.
ELEVATOR_INSTALLER_TEST_MODE=1 sh -c '
  . "$1"
  codesign() { return 1; }
  verify_macos_signature linux /tmp/elevator
' sh "$installer"

# macOS accepts a valid signature only when the pinned Team ID requirement is
# checked as well as the generic strict signature.
ELEVATOR_INSTALLER_TEST_MODE=1 sh -c '
  . "$1"
  codesign() {
    case "$*" in
      *T962D4K3Y7*) return 0 ;;
      *--strict*) return 0 ;;
      *) return 1 ;;
    esac
  }
  verify_macos_signature darwin /tmp/elevator
' sh "$installer"

if ELEVATOR_INSTALLER_TEST_MODE=1 sh -c '
  . "$1"
  codesign() {
    case "$*" in
      *T962D4K3Y7*) return 1 ;;
      *--strict*) return 0 ;;
      *) return 1 ;;
    esac
  }
  verify_macos_signature darwin /tmp/elevator
' sh "$installer" >/dev/null 2>&1; then
  echo 'expected a binary signed by the wrong team to be rejected' >&2
  exit 1
fi

echo 'installer tests passed'
