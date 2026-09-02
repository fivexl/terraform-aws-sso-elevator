package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// withTempHome points configFilePath's os.UserHomeDir() lookup at a fresh
// temp directory for the duration of the test, on both the Unix ($HOME) and
// Windows (%USERPROFILE%) lookup paths, so these tests never touch the real
// ~/.elevator/config.json.
func withTempHome(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", dir)
	return dir
}

func TestSaveConfigThenLoadConfigRoundTrips(t *testing.T) {
	withTempHome(t)
	const endpoint = "https://example.execute-api.us-east-1.amazonaws.com/default/access-requester-cli"

	path, err := saveConfig(cliConfig{Endpoint: endpoint})
	if err != nil {
		t.Fatalf("saveConfig: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("expected config file to exist at %s: %v", path, err)
	}

	cfg, err := loadConfig()
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	if cfg.Endpoint != endpoint {
		t.Errorf("got endpoint %q, want %q", cfg.Endpoint, endpoint)
	}
}

func TestSaveConfigLeavesNoTempFileBehind(t *testing.T) {
	// Regression test: saveConfig writes via a temp file + rename rather
	// than os.WriteFile's truncate-then-write, so a crash mid-write can't
	// leave config.json corrupted. That only actually protects anything if
	// the temp file is cleaned up on the success path too.
	dir := withTempHome(t)

	if _, err := saveConfig(cliConfig{Endpoint: "https://example.com"}); err != nil {
		t.Fatalf("saveConfig: %v", err)
	}

	entries, err := os.ReadDir(filepath.Join(dir, ".elevator"))
	if err != nil {
		t.Fatalf("read config dir: %v", err)
	}
	for _, e := range entries {
		if e.Name() != "config.json" {
			t.Errorf("unexpected leftover file %q in config dir -- the temp file from the atomic write should not survive a successful save", e.Name())
		}
	}
}

func TestSaveConfigOverwritesExistingConfig(t *testing.T) {
	withTempHome(t)

	if _, err := saveConfig(cliConfig{Endpoint: "https://old.example.com"}); err != nil {
		t.Fatalf("saveConfig (first): %v", err)
	}
	if _, err := saveConfig(cliConfig{Endpoint: "https://new.example.com"}); err != nil {
		t.Fatalf("saveConfig (second): %v", err)
	}

	cfg, err := loadConfig()
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	if cfg.Endpoint != "https://new.example.com" {
		t.Errorf("got endpoint %q, want the second save to have replaced the first", cfg.Endpoint)
	}
}

func TestSaveConfigSetsRestrictivePermissions(t *testing.T) {
	// Unix permission bits (0600/0700) aren't meaningful on Windows -- NTFS
	// doesn't map onto them the same way, and CI only ever runs this on
	// ubuntu-latest/macos-latest (cli-ci.yml), where this is real coverage.
	if runtime.GOOS == "windows" {
		t.Skip("Unix file permission bits aren't meaningful on Windows")
	}
	dir := withTempHome(t)

	path, err := saveConfig(cliConfig{Endpoint: "https://example.com"})
	if err != nil {
		t.Fatalf("saveConfig: %v", err)
	}

	fileInfo, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat config file: %v", err)
	}
	if perm := fileInfo.Mode().Perm(); perm != 0o600 {
		t.Errorf("config file permissions = %o, want 0600 -- it holds nothing else on the machine has any business reading", perm)
	}

	dirInfo, err := os.Stat(filepath.Join(dir, ".elevator"))
	if err != nil {
		t.Fatalf("stat config dir: %v", err)
	}
	if perm := dirInfo.Mode().Perm(); perm != 0o700 {
		t.Errorf("config dir permissions = %o, want 0700", perm)
	}
}

func TestLoadConfigReturnsZeroValueWhenFileDoesNotExist(t *testing.T) {
	withTempHome(t)

	cfg, err := loadConfig()
	if err != nil {
		t.Fatalf("unexpected error for a missing config file: %v", err)
	}
	if cfg.Endpoint != "" {
		t.Errorf("got endpoint %q, want empty", cfg.Endpoint)
	}
}
