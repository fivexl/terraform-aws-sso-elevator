package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"time"

	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/config"
)

// requestTimeout bounds a single attempt. HTTP APIs cap the API Gateway to
// Lambda integration at 30s, so this is set just above that ceiling — long
// enough that the server, not this client, is what times out first.
const requestTimeout = 35 * time.Second

// maxConnectAttempts bounds retries of connection-level failures only (DNS,
// refused connection, TLS handshake) — failures that happen before any
// request bytes reach the server, so nothing has been submitted yet and a
// retry can't double it up.
const maxConnectAttempts = 3

type requestPayload struct {
	Account       string `json:"account"`
	PermissionSet string `json:"permission_set"`
	Duration      string `json:"duration"`
	Reason        string `json:"reason"`
}

// runRequest implements the default `elevator --account ... --permission-set
// ... --duration ... --reason ...` submission flow. The request to the API
// Gateway endpoint is signed directly with the caller's own credentials —
// API Gateway's AWS_IAM authorizer verifies that signature itself and
// forwards the caller's identity to the Lambda, so there's no separate
// STS call to make or forward here.
func runRequest(args []string) {
	fs := flag.NewFlagSet("elevator", flag.ExitOnError)
	fs.Usage = func() { usage(fs.Output()) }
	account := fs.String("account", "", "AWS account ID to request access to (required)")
	permissionSet := fs.String("permission-set", "", "Permission set name to request (required)")
	duration := fs.String("duration", "", "How long access is needed, in hours (required)")
	reason := fs.String("reason", "", "Reason for the access request (required)")
	endpointFlag := fs.String("endpoint", "", "SSO Elevator API invoke URL (overrides ELEVATOR_ENDPOINT and the saved config file if set)")
	region := fs.String("region", "", "AWS region for SigV4 signing (defaults to the resolved AWS config region, falling back to us-east-1)")
	fs.Parse(args)

	if *account == "" || *permissionSet == "" || *duration == "" || *reason == "" {
		fmt.Fprintln(fs.Output(), "Usage: elevator --account ID --permission-set NAME --duration HOURS --reason TEXT [--endpoint URL]")
		fs.PrintDefaults()
		log.Fatal("--account, --permission-set, --duration, and --reason are required")
	}
	if hours, err := strconv.Atoi(*duration); err != nil || hours <= 0 {
		log.Fatalf("--duration must be a positive integer number of hours, got %q", *duration)
	}

	// Endpoint resolution, highest precedence first: --endpoint flag,
	// ELEVATOR_ENDPOINT env var (for scripts/automation that can't run the
	// interactive `configure` step), then the saved config file.
	endpoint := *endpointFlag
	if endpoint == "" {
		endpoint = os.Getenv("ELEVATOR_ENDPOINT")
	}
	if endpoint == "" {
		cfg, err := loadConfig()
		if err != nil {
			log.Fatalf("load config: %v", err)
		}
		endpoint = cfg.Endpoint
	}
	if endpoint == "" {
		log.Fatal("no --endpoint given, ELEVATOR_ENDPOINT not set, and none saved — run `elevator configure --endpoint URL` once, pass --endpoint, or set ELEVATOR_ENDPOINT")
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

	httpClient := &http.Client{Timeout: requestTimeout}
	resp, err := sendWithConnectRetry(httpClient, req)
	if err != nil {
		log.Fatalf("send request: %v (if this was a timeout waiting for a response, check Slack or the account's IAM Identity Center assignments before retrying — the request may have already gone through)", err)
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

// sendWithConnectRetry retries only when req never reached the server — a
// dial-level failure (DNS, connection refused, TLS handshake). A timeout
// waiting for a response is not retried here: by then the request may
// already be sitting in the Lambda, and retrying could submit a duplicate
// access request (and a duplicate auto-grant, if the caller self-approves).
func sendWithConnectRetry(client *http.Client, req *http.Request) (*http.Response, error) {
	var lastErr error
	for attempt := 1; attempt <= maxConnectAttempts; attempt++ {
		resp, err := client.Do(req)
		if err == nil {
			return resp, nil
		}
		lastErr = err
		if !isDialError(err) || attempt == maxConnectAttempts {
			return nil, err
		}
		backoff := time.Duration(attempt) * 2 * time.Second
		fmt.Printf("Connection attempt %d failed (%v), retrying in %s...\n", attempt, err, backoff)
		time.Sleep(backoff)
	}
	return nil, lastErr
}

// isDialError reports whether err is a failure to establish the connection
// at all, as opposed to a timeout or failure after the connection was made
// (by which point the request may have already reached the Lambda).
func isDialError(err error) bool {
	var opErr *net.OpError
	return errors.As(err, &opErr) && opErr.Op == "dial"
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
