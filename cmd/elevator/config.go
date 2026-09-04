package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
)

// cliConfig is the on-disk shape of ~/.elevator/config.json.
type cliConfig struct {
	Endpoint string `json:"endpoint"`
}

func configFilePath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolve home directory: %w", err)
	}
	return filepath.Join(home, ".elevator", "config.json"), nil
}

// loadConfig returns a zero-value cliConfig (no error) if the file doesn't
// exist yet — no config file is a normal, unconfigured state, not a failure.
func loadConfig() (cliConfig, error) {
	path, err := configFilePath()
	if err != nil {
		return cliConfig{}, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return cliConfig{}, nil
		}
		return cliConfig{}, fmt.Errorf("read config file %s: %w", path, err)
	}
	var cfg cliConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		return cliConfig{}, fmt.Errorf("parse config file %s: %w", path, err)
	}
	return cfg, nil
}

func saveConfig(cfg cliConfig) (string, error) {
	path, err := configFilePath()
	if err != nil {
		return "", err
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("create config directory: %w", err)
	}
	// MkdirAll's mode argument only applies to directories it actually
	// creates -- per its own docs, "if path is already a directory,
	// MkdirAll does nothing", silently leaving a pre-existing ~/.elevator
	// at whatever looser permissions it already had (e.g. 0755 from an
	// older version of this tool, or from umask effects) rather than the
	// 0700 the line above claims. Chmod unconditionally so this is
	// self-healing on every `configure` run, not just the first one.
	if err := os.Chmod(dir, 0o700); err != nil {
		return "", fmt.Errorf("set config directory permissions: %w", err)
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return "", fmt.Errorf("encode config: %w", err)
	}
	// Written to a temp file in the same directory, then renamed over the
	// real path, rather than os.WriteFile's truncate-then-write: a crash
	// mid-write, or two `elevator configure` runs racing each other, could
	// otherwise leave config.json truncated or interleaved -- the next
	// command would then fail to parse it, or silently lose the saved
	// endpoint. Same-directory is required for the rename to be atomic:
	// os.Rename only guarantees that within a single filesystem/volume.
	tmp, err := os.CreateTemp(dir, "config-*.json.tmp")
	if err != nil {
		return "", fmt.Errorf("create temp config file: %w", err)
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath) // no-op once the rename below succeeds
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return "", fmt.Errorf("write temp config file: %w", err)
	}
	// Without this, the write above can still be sitting in the OS's page
	// cache when the rename below returns -- the rename itself is atomic,
	// but a crash before the kernel independently flushes that page to disk
	// could still leave a zero-length or partially-written file readable
	// under the real path afterward, undermining the exact crash-protection
	// property this temp-file-then-rename approach exists for.
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return "", fmt.Errorf("sync temp config file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return "", fmt.Errorf("close temp config file: %w", err)
	}
	if err := os.Chmod(tmpPath, 0o600); err != nil {
		return "", fmt.Errorf("set config file permissions: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return "", fmt.Errorf("rename temp config file to %s: %w", path, err)
	}
	return path, nil
}

// runConfigure implements `elevator configure --endpoint URL`, saving the
// endpoint so subsequent `elevator` requests don't need --endpoint passed
// every time. Passing --endpoint on a request still overrides this file.
func runConfigure(args []string) {
	fs := flag.NewFlagSet("elevator configure", flag.ExitOnError)
	fs.Usage = func() { usage(fs.Output()) }
	endpoint := fs.String("endpoint", "", "SSO Elevator API invoke URL to save for future commands (required)")
	fs.Parse(args)

	if fs.NArg() > 0 {
		log.Fatalf("unrecognized argument(s): %v (did you mean --endpoint %s?)", fs.Args(), fs.Arg(0))
	}

	if *endpoint == "" {
		fmt.Fprintln(fs.Output(), "Usage: elevator configure --endpoint URL")
		fs.PrintDefaults()
		log.Fatal("--endpoint is required")
	}
	if err := validateEndpointScheme(*endpoint); err != nil {
		log.Fatal(err)
	}

	path, err := saveConfig(cliConfig{Endpoint: *endpoint})
	if err != nil {
		log.Fatalf("save config: %v", err)
	}
	fmt.Printf("Saved endpoint to %s\n", path)
}
