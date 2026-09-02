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
	"net/url"
	"os"
	"regexp"
	"strconv"
	"time"

	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/config"
)

// accountIDRE matches a well-formed 12-digit AWS account ID. Checked
// client-side purely so a typo'd account ID fails immediately instead of
// round-tripping to the server for the identical rejection.
var accountIDRE = regexp.MustCompile(`^\d{12}$`)

// executeAPIRegionRE matches API Gateway's own default invoke URL, which
// deterministically encodes the region the API is deployed in --
// <api-id>.execute-api.<region>.amazonaws.com[.cn]. Custom domains don't
// match this and fall back to --region / the AWS config region / us-east-1.
var executeAPIRegionRE = regexp.MustCompile(`\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$`)

// resolveSigningRegion picks the region to SigV4-sign with. A signature
// signed for the wrong region always fails API Gateway's AWS_IAM authorizer
// with SignatureDoesNotMatch, and the caller's own AWS profile/config region
// has no necessary relationship to where this particular API happens to be
// deployed -- so once an explicit --region override is ruled out, a region
// parsed straight out of the endpoint's own hostname is more reliable than
// falling through to the profile region or a hardcoded default.
func resolveSigningRegion(flagRegion, endpoint, configRegion string) string {
	if flagRegion != "" {
		return flagRegion
	}
	if u, err := url.Parse(endpoint); err == nil {
		if m := executeAPIRegionRE.FindStringSubmatch(u.Hostname()); m != nil {
			return m[1]
		}
	}
	if configRegion != "" {
		return configRegion
	}
	return "us-east-1"
}

// validateEndpointScheme rejects anything but https. A SigV4-signed request
// carries the caller's Authorization header and, for temporary credentials,
// X-Amz-Security-Token -- sending either over plain HTTP would put a live
// session token on the wire in the clear.
func validateEndpointScheme(endpoint string) error {
	u, err := url.Parse(endpoint)
	if err != nil {
		return fmt.Errorf("parse endpoint URL %q: %w", endpoint, err)
	}
	if u.Scheme != "https" {
		return fmt.Errorf("endpoint must use https://, got %q -- a signed request must never be sent over plain HTTP", endpoint)
	}
	return nil
}

// validateDurationHours reports whether s is a positive integer number of
// hours. The validated string itself, not a parsed int, is what actually
// gets sent to the server (main.py does its own int() parse) -- this only
// exists to fail fast client-side rather than round-trip a bad value.
func validateDurationHours(s string) error {
	hours, err := strconv.Atoi(s)
	if err != nil || hours <= 0 {
		return fmt.Errorf("--duration must be a positive integer number of hours, got %q", s)
	}
	return nil
}

// resolveEndpoint picks the API endpoint by precedence, highest first:
// --endpoint flag, ELEVATOR_ENDPOINT env var (for scripts/automation that
// can't run the interactive `configure` step), then the saved config file.
// Returns "" if none of the three provide one.
func resolveEndpoint(flagEndpoint, envEndpoint, configEndpoint string) string {
	if flagEndpoint != "" {
		return flagEndpoint
	}
	if envEndpoint != "" {
		return envEndpoint
	}
	return configEndpoint
}

// requestTimeout bounds a single attempt. HTTP APIs cap the API Gateway to
// Lambda integration at 30s, so this is set just above that ceiling — long
// enough that the server, not this client, is what times out first.
const requestTimeout = 35 * time.Second

// maxConnectAttempts bounds retries of connection-level failures only (DNS,
// refused connection, TLS handshake) — failures that happen before any
// request bytes reach the server, so nothing has been submitted yet and a
// retry can't double it up.
const maxConnectAttempts = 3

// doNotFollowRedirects makes any 3xx response the final response instead of
// being followed. A SigV4-signed request must never be replayed to a
// different URL: the default CheckRedirect follows up to 10 redirects, and
// a 307/308 would replay the full signed request — Authorization,
// X-Amz-Security-Token, and body — to whatever the redirect points at,
// while a 301/302/303 gets rewritten to a bodyless GET whose 200 response
// would read here as a successful submission even though nothing was
// actually sent.
func doNotFollowRedirects(*http.Request, []*http.Request) error {
	return http.ErrUseLastResponse
}

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

	if fs.NArg() > 0 {
		log.Fatalf("unrecognized argument(s): %v (all inputs are flags -- did you mean one of --account/--permission-set/--duration/--reason?)", fs.Args())
	}

	if *account == "" || *permissionSet == "" || *duration == "" || *reason == "" {
		fmt.Fprintln(fs.Output(), "Usage: elevator --account ID --permission-set NAME --duration HOURS --reason TEXT [--endpoint URL]")
		fs.PrintDefaults()
		log.Fatal("--account, --permission-set, --duration, and --reason are required")
	}
	if !accountIDRE.MatchString(*account) {
		log.Fatalf("--account must be a 12-digit AWS account ID, got %q", *account)
	}
	if err := validateDurationHours(*duration); err != nil {
		log.Fatal(err)
	}

	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	endpoint := resolveEndpoint(*endpointFlag, os.Getenv("ELEVATOR_ENDPOINT"), cfg.Endpoint)
	if endpoint == "" {
		log.Fatal("no --endpoint given, ELEVATOR_ENDPOINT not set, and none saved — run `elevator configure --endpoint URL` once, pass --endpoint, or set ELEVATOR_ENDPOINT")
	}
	if err := validateEndpointScheme(endpoint); err != nil {
		log.Fatal(err)
	}

	ctx := context.Background()

	awsCfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		log.Fatalf("load AWS config: %v", err)
	}

	resolvedRegion := resolveSigningRegion(*region, endpoint, awsCfg.Region)

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

	httpClient := &http.Client{Timeout: requestTimeout, CheckRedirect: doNotFollowRedirects}
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
	serverFailed, serverMessage := parseSubmissionResponse(respBody)
	if serverFailed {
		log.Fatalf("request was not submitted: %s", serverMessage)
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

// parseSubmissionResponse reports whether the server explicitly flagged
// this request as not actually submitted, despite the 2xx status that got
// us here, plus the best available message to show for it. OK is a *bool,
// not bool, so a response that omits "ok" entirely isn't mistaken for an
// explicit false — only "ok": false is treated as a real failure signal.
func parseSubmissionResponse(respBody []byte) (failed bool, message string) {
	var parsed struct {
		OK      *bool  `json:"ok"`
		Message string `json:"message"`
	}
	message = string(respBody)
	if json.Unmarshal(respBody, &parsed) != nil {
		return false, message
	}
	if parsed.Message != "" {
		message = parsed.Message
	}
	return parsed.OK != nil && !*parsed.OK, message
}
