package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
)

var updateGolden = flag.Bool("update", false, "rewrite tests/fixtures/sts_proof/valid_envelope.json")

// goldenPath is shared with src/tests/test_cli_proof.py, which verifies this
// exact envelope with the Lambda's parser.
var goldenPath = filepath.Join("..", "..", "tests", "fixtures", "sts_proof", "valid_envelope.json")

// goldenVector holds fixed inputs (AWS documentation example credentials,
// not real ones) and the envelope the CLI must build from them.
type goldenVector struct {
	APIID           string `json:"api_id"`
	Region          string `json:"region"`
	SigningTime     string `json:"signing_time"`
	AccessKeyID     string `json:"access_key_id"`
	SecretAccessKey string `json:"secret_access_key"`
	SessionToken    string `json:"session_token"`
	Nonce           string `json:"nonce"`
	Payload         string `json:"payload"`
	PayloadSHA256   string `json:"payload_sha256"`
	Body            string `json:"body"`
}

func goldenInputs(t *testing.T) (goldenVector, []byte) {
	t.Helper()
	payload, err := json.Marshal(requestPayload{
		Account:       "111111111111",
		PermissionSet: "ReadOnly",
		Duration:      "60",
		Reason:        `incident <42> & "quotes" ü`,
	})
	if err != nil {
		t.Fatal(err)
	}
	return goldenVector{
		APIID:           "abcde12345",
		Region:          "eu-central-1",
		SigningTime:     "2026-01-15T12:00:00Z",
		AccessKeyID:     "ASIAIOSFODNN7EXAMPLE",
		SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
		SessionToken:    "FAKE+SESSION/TOKEN=FOR-TESTS",
		Nonce:           "00112233445566778899aabbccddeeff",
		Payload:         string(payload),
	}, payload
}

func buildGolden(t *testing.T) goldenVector {
	t.Helper()
	v, payload := goldenInputs(t)
	signingTime, err := time.Parse(time.RFC3339, v.SigningTime)
	if err != nil {
		t.Fatal(err)
	}
	creds := aws.Credentials{AccessKeyID: v.AccessKeyID, SecretAccessKey: v.SecretAccessKey, SessionToken: v.SessionToken}
	body, err := buildEnvelope(context.Background(), creds, payload, v.APIID, v.Region, v.Nonce, signingTime)
	if err != nil {
		t.Fatalf("buildEnvelope: %v", err)
	}
	sum := sha256.Sum256(payload)
	v.PayloadSHA256 = hex.EncodeToString(sum[:])
	v.Body = string(body)
	return v
}

func TestGoldenEnvelope(t *testing.T) {
	got, err := json.MarshalIndent(buildGolden(t), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	if *updateGolden {
		if err := os.WriteFile(goldenPath, got, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("read golden vector (regenerate with go test -run TestGoldenEnvelope -update): %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Errorf("envelope differs from %s; if the wire contract changed on purpose, regenerate it with -update and update src/cli_proof.py\ngot:\n%s", goldenPath, got)
	}
}

func decodeEnvelope(t *testing.T, body string) (proofEnvelope, url.Values) {
	t.Helper()
	var env proofEnvelope
	dec := json.NewDecoder(strings.NewReader(body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&env); err != nil {
		t.Fatalf("decode envelope: %v", err)
	}
	u, err := url.Parse(env.Proof.URL)
	if err != nil {
		t.Fatalf("parse proof url: %v", err)
	}
	if u.Scheme != "https" || u.Host != "sts.eu-central-1.amazonaws.com" || u.Path != "/" {
		t.Errorf("proof url = %s, want https://sts.eu-central-1.amazonaws.com/", env.Proof.URL)
	}
	return env, u.Query()
}

func TestBuildEnvelopeContents(t *testing.T) {
	v := buildGolden(t)
	env, q := decodeEnvelope(t, v.Body)

	if env.Version != 1 || env.Payload != v.Payload {
		t.Errorf("version/payload = %d/%q, want 1/%q", env.Version, env.Payload, v.Payload)
	}
	wantHeaders := map[string]string{
		"x-elevator-payload-sha256": v.PayloadSHA256,
		"x-elevator-server-id":      "abcde12345",
		"x-elevator-nonce":          v.Nonce,
	}
	if len(env.Proof.Headers) != len(wantHeaders) {
		t.Errorf("headers = %v, want exactly %v", env.Proof.Headers, wantHeaders)
	}
	for k, want := range wantHeaders {
		if got := env.Proof.Headers[k]; got != want {
			t.Errorf("header %s = %q, want %q", k, got, want)
		}
	}
	wantQuery := map[string]string{
		"Action":               "GetCallerIdentity",
		"Version":              "2011-06-15",
		"X-Amz-Algorithm":      "AWS4-HMAC-SHA256",
		"X-Amz-Credential":     "ASIAIOSFODNN7EXAMPLE/20260115/eu-central-1/sts/aws4_request",
		"X-Amz-Date":           "20260115T120000Z",
		"X-Amz-Expires":        "60",
		"X-Amz-Security-Token": "FAKE+SESSION/TOKEN=FOR-TESTS",
		"X-Amz-SignedHeaders":  "host;x-elevator-nonce;x-elevator-payload-sha256;x-elevator-server-id",
	}
	for k, want := range wantQuery {
		if got := q[k]; len(got) != 1 || got[0] != want {
			t.Errorf("query %s = %v, want [%q]", k, got, want)
		}
	}
	if len(q) != len(wantQuery)+1 || len(q.Get("X-Amz-Signature")) != 64 {
		t.Errorf("query = %v, want exactly %v plus a 64-char X-Amz-Signature", q, wantQuery)
	}
}

func TestBuildEnvelopeHashCoversExactPayloadBytes(t *testing.T) {
	creds := aws.Credentials{AccessKeyID: "ASIAIOSFODNN7EXAMPLE", SecretAccessKey: "secret"}
	payload := []byte(`{"account":"111111111111","reason":"a"}`)
	body, err := buildEnvelope(context.Background(), creds, payload, "abcde12345", "eu-central-1", "00112233445566778899aabbccddeeff", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	env, q := decodeEnvelope(t, string(body))
	sum := sha256.Sum256(payload)
	if env.Payload != string(payload) || env.Proof.Headers["x-elevator-payload-sha256"] != hex.EncodeToString(sum[:]) {
		t.Errorf("payload %q / hash %q do not match the input bytes", env.Payload, env.Proof.Headers["x-elevator-payload-sha256"])
	}
	if _, ok := q["X-Amz-Security-Token"]; ok {
		t.Error("long-term credentials must not produce an X-Amz-Security-Token")
	}
}

func TestResolveAPIID(t *testing.T) {
	cases := []struct {
		name, endpoint, flag, env, saved, want string
		wantErr                                bool
	}{
		{name: "execute-api URL names the API", endpoint: "https://abcde12345.execute-api.eu-west-1.amazonaws.com/default/access-requester-cli", want: "abcde12345"},
		{name: "uppercase host", endpoint: "https://ABCDE12345.EXECUTE-API.EU-WEST-1.AMAZONAWS.COM/default/x", want: "abcde12345"},
		{name: "execute-api URL wins over configured id", endpoint: "https://abcde12345.execute-api.eu-west-1.amazonaws.com/x", flag: "zzzzz99999", want: "abcde12345"},
		{name: "custom domain without id is refused", endpoint: "https://elevator.example.com/cli", wantErr: true},
		{name: "custom domain with flag", endpoint: "https://elevator.example.com/cli", flag: "abcde12345", env: "bbbbb22222", saved: "ccccc33333", want: "abcde12345"},
		{name: "custom domain with env", endpoint: "https://elevator.example.com/cli", env: "bbbbb22222", saved: "ccccc33333", want: "bbbbb22222"},
		{name: "custom domain with saved id", endpoint: "https://elevator.example.com/cli", saved: "ccccc33333", want: "ccccc33333"},
		{name: "malformed id is refused", endpoint: "https://elevator.example.com/cli", flag: "not-an-id", wantErr: true},
		{name: "lookalike host is a custom domain", endpoint: "https://abcde12345.execute-api.eu-west-1.amazonaws.com.evil.example/x", wantErr: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := resolveAPIID(c.endpoint, c.flag, c.env, c.saved)
			if (err != nil) != c.wantErr || got != c.want {
				t.Errorf("resolveAPIID = %q, %v; want %q, error %v", got, err, c.want, c.wantErr)
			}
		})
	}
}

func TestResolveAPIIDErrorExplainsTheFix(t *testing.T) {
	_, err := resolveAPIID("https://elevator.example.com/cli", "", "", "")
	if err == nil || !strings.Contains(err.Error(), "elevator configure --api-id") {
		t.Errorf("error = %v, want it to point at `elevator configure --api-id`", err)
	}
}

func TestNewNonce(t *testing.T) {
	a, err := newNonce()
	if err != nil {
		t.Fatal(err)
	}
	b, _ := newNonce()
	if len(a) != 32 || strings.ToLower(a) != a || a == b {
		t.Errorf("nonces %q, %q: want two different 32-char lowercase hex strings", a, b)
	}
}
