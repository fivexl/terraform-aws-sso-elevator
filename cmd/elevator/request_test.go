package main

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// roundTripperFunc adapts a plain function to http.RoundTripper, so a test
// can script a client's per-attempt behavior without any real networking.
type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestValidateEndpointSchemeRejectsPlainHTTP(t *testing.T) {
	if err := validateEndpointScheme("http://example.execute-api.us-east-1.amazonaws.com/default/access-requester-cli"); err == nil {
		t.Fatal("expected an error for a plain-http endpoint, a SigV4-signed request must never be sent over it")
	}
}

func TestValidateEndpointSchemeAcceptsHTTPS(t *testing.T) {
	if err := validateEndpointScheme("https://example.execute-api.us-east-1.amazonaws.com/default/access-requester-cli"); err != nil {
		t.Fatalf("unexpected error for a valid https endpoint: %v", err)
	}
}

func TestValidateEndpointSchemeRejectsUnparseableURL(t *testing.T) {
	if err := validateEndpointScheme("://not a url"); err == nil {
		t.Fatal("expected an error for an unparseable endpoint")
	}
}

// TestValidateEndpointSchemeRejectsMissingHost is a regression test: url.Parse
// still reports Scheme "https" for each of these single-slash paste errors,
// so a check that only looked at Scheme let `configure` persist garbage with
// exit 0.
func TestValidateEndpointSchemeRejectsMissingHost(t *testing.T) {
	for _, endpoint := range []string{"https:/foo", "https://", "https:///path"} {
		if err := validateEndpointScheme(endpoint); err == nil {
			t.Errorf("validateEndpointScheme(%q) = nil, want an error for a missing host", endpoint)
		}
	}
}

func TestResolveSigningRegion(t *testing.T) {
	cases := []struct {
		name         string
		flagRegion   string
		endpoint     string
		configRegion string
		want         string
	}{
		{
			name:     "derives region from a real execute-api endpoint",
			endpoint: "https://abc123.execute-api.eu-west-1.amazonaws.com/default/access-requester-cli",
			want:     "eu-west-1",
		},
		{
			name:       "explicit --region overrides an endpoint that would otherwise resolve differently",
			flagRegion: "ap-southeast-2",
			endpoint:   "https://abc123.execute-api.eu-west-1.amazonaws.com/default/access-requester-cli",
			want:       "ap-southeast-2",
		},
		{
			name:         "falls back to the AWS config region for a custom domain endpoint",
			endpoint:     "https://elevator.example.com/access-requester-cli",
			configRegion: "sa-east-1",
			want:         "sa-east-1",
		},
		{
			name:     "falls back to us-east-1 when nothing else resolves",
			endpoint: "https://elevator.example.com/access-requester-cli",
			want:     "us-east-1",
		},
		{
			name:     "derives region from an execute-api.amazonaws.com.cn endpoint",
			endpoint: "https://abc123.execute-api.cn-north-1.amazonaws.com.cn/default/access-requester-cli",
			want:     "cn-north-1",
		},
		{
			// Regression test: url.Parse doesn't lowercase the host, and the
			// underlying regex is lowercase-only -- an uppercase (e.g.
			// pasted straight from the AWS console) endpoint used to miss
			// the match entirely and silently fall through to us-east-1.
			name:     "derives region from an execute-api endpoint regardless of case",
			endpoint: "HTTPS://ABC123.EXECUTE-API.EU-WEST-1.AMAZONAWS.COM/default/access-requester-cli",
			want:     "eu-west-1",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := resolveSigningRegion(c.flagRegion, c.endpoint, c.configRegion)
			if got != c.want {
				t.Errorf("resolveSigningRegion(%q, %q, %q) = %q, want %q", c.flagRegion, c.endpoint, c.configRegion, got, c.want)
			}
		})
	}
}

// TestDoNotFollowRedirectsStopsAt3xx is a regression test for the actual bug:
// a SigV4-signed request must never be replayed to a redirect target. A
// 307/308 replays the full signed request (Authorization header, session
// token, body) to wherever the redirect points; a 301/302/303 gets
// rewritten to a bodyless GET, whose 200 would otherwise be read here as a
// successful submission even though nothing was actually sent.
func TestDoNotFollowRedirectsStopsAt3xx(t *testing.T) {
	// atomic.Bool, not a plain bool: written from the target server's own
	// handler goroutine, read from the test goroutine. -race only passes on
	// a plain bool here because the handler never actually runs in the
	// passing case (the redirect isn't followed) -- if the regression this
	// guards against ever returns, the handler starts running concurrently
	// with the read below, and a plain bool turns that into a race report
	// instead of this test's own assertion message.
	var redirectTargetHit atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		redirectTargetHit.Store(true)
		w.WriteHeader(http.StatusOK)
	}))
	defer target.Close()

	for _, code := range []int{
		http.StatusMovedPermanently,  // 301
		http.StatusFound,             // 302
		http.StatusSeeOther,          // 303
		http.StatusTemporaryRedirect, // 307
		http.StatusPermanentRedirect, // 308
	} {
		t.Run(http.StatusText(code), func(t *testing.T) {
			redirectTargetHit.Store(false)
			source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Redirect(w, r, target.URL, code)
			}))
			defer source.Close()

			client := &http.Client{CheckRedirect: doNotFollowRedirects}
			resp, err := client.Get(source.URL)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			defer resp.Body.Close()

			if resp.StatusCode != code {
				t.Fatalf("got status %d, want the redirect's own %d (it should not have been followed)", resp.StatusCode, code)
			}
			if redirectTargetHit.Load() {
				t.Fatal("the redirect target was hit -- the signed request was replayed to it")
			}
		})
	}
}

// TestParseSubmissionResponse is a regression test: OK was decoded and then
// never read, so a 2xx body of {"ok":false,...} printed "✓ Request
// submitted." — a false positive.
func TestParseSubmissionResponse(t *testing.T) {
	cases := []struct {
		name        string
		body        string
		wantFailed  bool
		wantMessage string
	}{
		{name: "ok true", body: `{"ok":true,"message":"posted for approval"}`, wantFailed: false, wantMessage: "posted for approval"},
		{name: "ok false", body: `{"ok":false,"message":"duplicate request"}`, wantFailed: true, wantMessage: "duplicate request"},
		{name: "ok omitted entirely is not treated as false", body: `{"message":"legacy server"}`, wantFailed: false, wantMessage: "legacy server"},
		{name: "non-json body falls back to the raw body, never flagged failed", body: `not json`, wantFailed: false, wantMessage: "not json"},
		{name: "json with no message falls back to the raw body", body: `{"ok":true}`, wantFailed: false, wantMessage: `{"ok":true}`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			failed, message := parseSubmissionResponse([]byte(c.body))
			if failed != c.wantFailed {
				t.Errorf("failed = %v, want %v", failed, c.wantFailed)
			}
			if message != c.wantMessage {
				t.Errorf("message = %q, want %q", message, c.wantMessage)
			}
		})
	}
}

// TestIsDialError is a regression test for the single invariant that keeps a
// connection-level failure from ever being retried past the point where a
// duplicate submission (and duplicate auto-grant, for a self-approving
// caller) becomes possible: only a failure to establish the connection at
// all is safe to retry, since nothing has reached the server yet.
func TestIsDialError(t *testing.T) {
	t.Run("real dial failure to a closed port is a dial error", func(t *testing.T) {
		// Listen then immediately close, rather than a hardcoded port
		// number, so nothing else on the test runner could coincidentally
		// be listening there.
		l, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatalf("listen: %v", err)
		}
		addr := l.Addr().String()
		l.Close()

		_, dialErr := net.Dial("tcp", addr)
		if dialErr == nil {
			t.Fatal("expected an error connecting to a just-closed port")
		}
		if !isDialError(dialErr) {
			t.Errorf("isDialError(%v) = false, want true for a real connection-refused error", dialErr)
		}
	})

	t.Run("a net.OpError with a non-dial Op is not a dial error", func(t *testing.T) {
		// This is what a failure *after* the connection was already
		// established looks like -- by this point the request may already
		// be sitting in the Lambda, so it must not be treated as safe to
		// retry the same way a dial failure is.
		err := &net.OpError{Op: "read", Net: "tcp", Err: errors.New("connection reset")}
		if isDialError(err) {
			t.Error("isDialError = true, want false for an Op other than \"dial\"")
		}
	})

	t.Run("a plain non-net error is not a dial error", func(t *testing.T) {
		if isDialError(errors.New("some other failure")) {
			t.Error("isDialError = true, want false for a non-*net.OpError")
		}
	})

	t.Run("nil error is not a dial error", func(t *testing.T) {
		if isDialError(nil) {
			t.Error("isDialError(nil) = true, want false")
		}
	})
}

// TestSendWithConnectRetryDoesNotRetryAfterTheServerWasReached is a
// regression test for the invariant TestIsDialError only checks in
// isolation: sendWithConnectRetry itself must not retry once the connection
// was actually established, even though the request then failed (here, by
// timing out waiting for a response) -- retrying past that point risks a
// duplicate submission (and duplicate auto-grant, for a self-approving
// caller), since the first attempt may already have reached the Lambda.
func TestSendWithConnectRetryDoesNotRetryAfterTheServerWasReached(t *testing.T) {
	var hits int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		time.Sleep(200 * time.Millisecond) // outlast the client timeout
	}))
	defer srv.Close()
	// A nil body, not bytes.NewReader([]byte("{}")): sendWithConnectRetry
	// reuses the same *http.Request across attempts without restoring the
	// body from req.GetBody, so a non-empty body reader is already
	// exhausted by attempt 2 -- that attempt then fails inside the
	// transport on a Content-Length mismatch, before ever reaching the
	// network, making hits stay 1 regardless of whether the retry
	// predicate under test is actually correct. Confirmed: with the old
	// body, this test still passed even after deliberately breaking
	// isDialError's predicate to also retry on os.IsTimeout (the exact
	// regression it exists to catch).
	req, _ := http.NewRequest(http.MethodPost, srv.URL, nil)
	if _, err := sendWithConnectRetry(&http.Client{Timeout: 50 * time.Millisecond}, req); err == nil {
		t.Fatal("expected a timeout")
	}
	if n := atomic.LoadInt32(&hits); n != 1 {
		t.Fatalf("server hit %d times, want exactly 1", n)
	}
}

// TestSendWithConnectRetryRetriesAfterADialFailure is the companion this
// invariant was still missing: proof that a genuine dial failure (the one
// case sendWithConnectRetry exists to retry) actually gets retried and can
// succeed, not just that a non-dial failure doesn't. Scripted via a fake
// RoundTripper rather than real sockets, so it isn't subject to OS-level
// port-reuse timing the way simulating this with real listeners would be.
func TestSendWithConnectRetryRetriesAfterADialFailure(t *testing.T) {
	var attempts int32
	client := &http.Client{
		Transport: roundTripperFunc(func(*http.Request) (*http.Response, error) {
			if atomic.AddInt32(&attempts, 1) == 1 {
				// The exact shape isDialError checks for: op == "dial".
				return nil, &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("connection refused")}
			}
			return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("")), Header: http.Header{}}, nil
		}),
	}
	req, _ := http.NewRequest(http.MethodPost, "http://elevator.invalid/", nil)

	resp, err := sendWithConnectRetry(client, req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("got status %d, want %d", resp.StatusCode, http.StatusOK)
	}
	if n := atomic.LoadInt32(&attempts); n != 2 {
		t.Fatalf("got %d attempts, want exactly 2 (one dial failure, then a retry that reached the server)", n)
	}
}

func TestResolveEndpoint(t *testing.T) {
	cases := []struct {
		name                                      string
		flagEndpoint, envEndpoint, configEndpoint string
		want                                      string
	}{
		{name: "flag wins over env and config", flagEndpoint: "https://flag", envEndpoint: "https://env", configEndpoint: "https://config", want: "https://flag"},
		{name: "env wins over config when flag is unset", envEndpoint: "https://env", configEndpoint: "https://config", want: "https://env"},
		{name: "falls back to config when neither flag nor env is set", configEndpoint: "https://config", want: "https://config"},
		{name: "empty when nothing is configured anywhere", want: ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := resolveEndpoint(c.flagEndpoint, c.envEndpoint, c.configEndpoint)
			if got != c.want {
				t.Errorf("resolveEndpoint(%q, %q, %q) = %q, want %q", c.flagEndpoint, c.envEndpoint, c.configEndpoint, got, c.want)
			}
		})
	}
}

func TestValidateDurationMinutes(t *testing.T) {
	// No 30-minute-increment requirement, and no client-side upper bound --
	// any positive whole number is valid here; the server enforces the
	// actual configured maximum.
	for _, v := range []string{"1", "2", "24", "47", "100", "1440"} {
		if err := validateDurationMinutes(v); err != nil {
			t.Errorf("validateDurationMinutes(%q) = %v, want nil", v, err)
		}
	}
	for _, v := range []string{"0", "-1", "1.5", "abc", "", "2h", "2 "} {
		if err := validateDurationMinutes(v); err == nil {
			t.Errorf("validateDurationMinutes(%q) = nil, want an error", v)
		}
	}
}

func TestAccountIDRE(t *testing.T) {
	for _, v := range []string{"123456789012", "000000000000"} {
		if !accountIDRE.MatchString(v) {
			t.Errorf("accountIDRE.MatchString(%q) = false, want true", v)
		}
	}
	for _, v := range []string{"12345678901", "1234567890123", "12345678901a", "", "123456789012 ", " 123456789012"} {
		if accountIDRE.MatchString(v) {
			t.Errorf("accountIDRE.MatchString(%q) = true, want false", v)
		}
	}
}

// TestRequestPayloadJSONShape locks in the wire contract src/main.py's
// handle_cli_access_request actually parses (body.get("account"),
// body.get("permission_set"), body.get("duration"), body.get("reason")) --
// a renamed Go struct tag here would silently break every field the server
// reads without any test on either side catching it.
func TestRequestPayloadJSONShape(t *testing.T) {
	body, err := json.Marshal(requestPayload{
		Account:       "123456789012",
		PermissionSet: "AdminAccess",
		Duration:      "2",
		Reason:        "testing",
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var decoded map[string]any
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	want := map[string]any{
		"account":        "123456789012",
		"permission_set": "AdminAccess",
		"duration":       "2",
		"reason":         "testing",
	}
	for key, wantVal := range want {
		if got, ok := decoded[key]; !ok || got != wantVal {
			t.Errorf("field %q = %v, want %v", key, decoded[key], wantVal)
		}
	}
	if len(decoded) != len(want) {
		t.Errorf("got %d top-level fields %v, want exactly %v", len(decoded), decoded, want)
	}
}
