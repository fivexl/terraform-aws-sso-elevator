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
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", fmt.Errorf("create config directory: %w", err)
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return "", fmt.Errorf("encode config: %w", err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return "", fmt.Errorf("write config file %s: %w", path, err)
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

	if *endpoint == "" {
		fmt.Fprintln(fs.Output(), "Usage: elevator configure --endpoint URL")
		fs.PrintDefaults()
		log.Fatal("--endpoint is required")
	}

	path, err := saveConfig(cliConfig{Endpoint: *endpoint})
	if err != nil {
		log.Fatalf("save config: %v", err)
	}
	fmt.Printf("Saved endpoint to %s\n", path)
}
