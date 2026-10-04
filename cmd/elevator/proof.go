package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
)

// The identity proof the Lambda verifies (wire contract v1, specified in
// src/cli_proof.py's module docstring; tests/fixtures/sts_proof/ holds the
// vector both ends test against). API Gateway's own SigV4 check cannot be
// relied on for identity, because a direct Lambda invoke can forge the
// requestContext it produces.
const (
	envelopeVersion = 1
	// proofExpires is X-Amz-Expires. STS ignores it; the Lambda enforces
	// its own 60 s window on X-Amz-Date.
	proofExpires = "60"

	payloadHashHeader = "X-Elevator-Payload-Sha256"
	serverIDHeader    = "X-Elevator-Server-Id"
	nonceHeader       = "X-Elevator-Nonce"
)

// emptySHA256 is the SHA-256 of the empty body of the presigned GET.
const emptySHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

// executeAPIHostRE matches API Gateway's default invoke host and captures
// the REST API id, which is the proof's audience.
var executeAPIHostRE = regexp.MustCompile(`^([a-z0-9]+)\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$`)

// apiIDRE matches an API Gateway REST API id.
var apiIDRE = regexp.MustCompile(`^[a-z0-9]{10}$`)

type proofEnvelope struct {
	Version int      `json:"version"`
	Payload string   `json:"payload"`
	Proof   stsProof `json:"proof"`
}

type stsProof struct {
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers"`
}

// resolveAPIID returns the REST API id the proof is addressed to. A default
// execute-api URL names it; any other host (a custom domain) needs it
// configured, because the Lambda accepts only proofs for its own API.
func resolveAPIID(endpoint, flagAPIID, envAPIID, configAPIID string) (string, error) {
	u, err := url.Parse(endpoint)
	if err != nil {
		return "", fmt.Errorf("parse endpoint URL %q: %w", endpoint, err)
	}
	if m := executeAPIHostRE.FindStringSubmatch(strings.ToLower(u.Hostname())); m != nil {
		return m[1], nil
	}
	apiID := firstSet(flagAPIID, envAPIID, configAPIID)
	if apiID == "" {
		return "", fmt.Errorf("endpoint %q is not an execute-api URL, so the REST API id must be configured: run `elevator configure --api-id ID` (Terraform output requester_api_id), pass --api-id, or set ELEVATOR_API_ID", endpoint)
	}
	if err := validateAPIID(apiID); err != nil {
		return "", err
	}
	return apiID, nil
}

func validateAPIID(apiID string) error {
	if !apiIDRE.MatchString(apiID) {
		return fmt.Errorf("API id must be 10 lowercase letters or digits (Terraform output requester_api_id), got %q", apiID)
	}
	return nil
}

func newNonce() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate nonce: %w", err)
	}
	return hex.EncodeToString(b), nil
}

func stsEndpoint(region string) string {
	return "https://sts." + region + ".amazonaws.com/"
}

// buildEnvelope presigns sts:GetCallerIdentity with headers binding the
// payload, audience and nonce, and wraps it with payload into the body sent
// to API Gateway. creds must be the same credentials that sign that request.
func buildEnvelope(ctx context.Context, creds aws.Credentials, payload []byte, apiID, region, nonce string, now time.Time) ([]byte, error) {
	sum := sha256.Sum256(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, stsEndpoint(region), nil)
	if err != nil {
		return nil, fmt.Errorf("create STS request: %w", err)
	}
	q := url.Values{}
	q.Set("Action", "GetCallerIdentity")
	q.Set("Version", "2011-06-15")
	q.Set("X-Amz-Expires", proofExpires)
	req.URL.RawQuery = q.Encode()
	req.Header.Set(payloadHashHeader, hex.EncodeToString(sum[:]))
	req.Header.Set(serverIDHeader, apiID)
	req.Header.Set(nonceHeader, nonce)

	signedURL, signedHeaders, err := v4.NewSigner().PresignHTTP(ctx, creds, req, emptySHA256, "sts", region, now.UTC())
	if err != nil {
		return nil, fmt.Errorf("presign STS GetCallerIdentity: %w", err)
	}
	headers := map[string]string{}
	for name, values := range signedHeaders {
		lower := strings.ToLower(name)
		if lower == "host" {
			continue // the Lambda derives it from the URL
		}
		headers[lower] = strings.Join(values, ",")
	}
	// SetEscapeHTML(false) keeps the URL's "&" literal; the Lambda decodes either form.
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(proofEnvelope{
		Version: envelopeVersion,
		Payload: string(payload),
		Proof:   stsProof{URL: signedURL, Headers: headers},
	}); err != nil {
		return nil, fmt.Errorf("encode request envelope: %w", err)
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}
