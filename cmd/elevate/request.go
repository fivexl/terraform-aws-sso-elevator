package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/config"
)

type requestPayload struct {
	Account       string `json:"account"`
	PermissionSet string `json:"permission_set"`
	Duration      string `json:"duration"`
	Reason        string `json:"reason"`
}

// runRequest implements the default `elevate --account ... --permission-set
// ... --duration ... --reason ...` submission flow. The request to the API
// Gateway endpoint is signed directly with the caller's own credentials —
// API Gateway's AWS_IAM authorizer verifies that signature itself and
// forwards the caller's identity to the Lambda, so there's no separate
// STS call to make or forward here.
func runRequest(args []string) {
	fs := flag.NewFlagSet("elevate", flag.ExitOnError)
	fs.Usage = func() { usage(fs.Output()) }
	account := fs.String("account", "", "AWS account ID to request access to (required)")
	permissionSet := fs.String("permission-set", "", "Permission set name to request (required)")
	duration := fs.String("duration", "", "How long access is needed, in hours (required)")
	reason := fs.String("reason", "", "Reason for the access request (required)")
	endpointFlag := fs.String("endpoint", "", "SSO Elevator API invoke URL (overrides ELEVATE_ENDPOINT and the saved config file if set)")
	region := fs.String("region", "", "AWS region for SigV4 signing (defaults to the resolved AWS config region, falling back to us-east-1)")
	fs.Parse(args)

	if *account == "" || *permissionSet == "" || *duration == "" || *reason == "" {
		fmt.Fprintln(fs.Output(), "Usage: elevate --account ID --permission-set NAME --duration HOURS --reason TEXT [--endpoint URL]")
		fs.PrintDefaults()
		log.Fatal("--account, --permission-set, --duration, and --reason are required")
	}
	if hours, err := strconv.Atoi(*duration); err != nil || hours <= 0 {
		log.Fatalf("--duration must be a positive integer number of hours, got %q", *duration)
	}

	// Endpoint resolution, highest precedence first: --endpoint flag,
	// ELEVATE_ENDPOINT env var (for scripts/automation that can't run the
	// interactive `configure` step), then the saved config file.
	endpoint := *endpointFlag
	if endpoint == "" {
		endpoint = os.Getenv("ELEVATE_ENDPOINT")
	}
	if endpoint == "" {
		cfg, err := loadConfig()
		if err != nil {
			log.Fatalf("load config: %v", err)
		}
		endpoint = cfg.Endpoint
	}
	if endpoint == "" {
		log.Fatal("no --endpoint given, ELEVATE_ENDPOINT not set, and none saved — run `elevate configure --endpoint URL` once, pass --endpoint, or set ELEVATE_ENDPOINT")
	}

	ctx := context.Background()

	awsCfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		log.Fatalf("load AWS config: %v", err)
	}

	resolvedRegion := *region
	if resolvedRegion == "" {
		resolvedRegion = awsCfg.Region
	}
	if resolvedRegion == "" {
		resolvedRegion = "us-east-1"
	}

	creds, err := awsCfg.Credentials.Retrieve(ctx)
	if err != nil {
		log.Fatalf("retrieve credentials: %v", err)
	}

	body, err := json.Marshal(requestPayload{
		Account:       *account,
		PermissionSet: *permissionSet,
		Duration:      *duration,
		Reason:        *reason,
	})
	if err != nil {
		log.Fatalf("encode request body: %v", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		log.Fatalf("create request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")

	sum := sha256.Sum256(body)
	payloadHash := hex.EncodeToString(sum[:])

	if err := v4.NewSigner().SignHTTP(ctx, creds, req, payloadHash, "execute-api", resolvedRegion, time.Now().UTC()); err != nil {
		log.Fatalf("sign request: %v", err)
	}

	fmt.Printf("Credential source: %s\n", creds.Source)
	fmt.Printf("POST %s\n\n", endpoint)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Fatalf("send request: %v", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Fatalf("read response: %v", err)
	}

	if resp.StatusCode >= 300 {
		fmt.Printf("Status: %s\n", resp.Status)
		fmt.Printf("Body:\n%s\n", string(respBody))
		log.Fatalf("request failed with status %s", resp.Status)
	}

	printSubmissionResult(*account, *permissionSet, *duration, respBody)
}

// printSubmissionResult prints an unambiguous "what happens next" message on
// a successful (2xx) submission. The server returns the identical message
// whether the request ends up pending a human's Slack click or gets granted
// immediately via self-approval (confirmed against the real deployment —
// the grant executes synchronously before the Lambda responds in that case),
// so this deliberately does NOT claim a human decision is pending — only
// that this command hasn't waited for or confirmed the actual outcome.
func printSubmissionResult(account, permissionSet, duration string, respBody []byte) {
	var parsed struct {
		OK      bool   `json:"ok"`
		Message string `json:"message"`
	}
	serverMessage := string(respBody)
	if err := json.Unmarshal(respBody, &parsed); err == nil && parsed.Message != "" {
		serverMessage = parsed.Message
	}

	fmt.Printf(`✓ Request submitted.
  Account: %s · Permission set: %s · Duration: %sh
  Server response: %s

This command does NOT wait for or confirm a decision. Your request has
been posted to the approval workflow in Slack — it may be granted
automatically (if you're a self-approving approver) or require someone
else to click Approve/Deny. Check Slack, or the account's IAM Identity
Center assignments, to confirm the actual outcome before assuming access
has been granted.
`, account, permissionSet, duration, serverMessage)
}
