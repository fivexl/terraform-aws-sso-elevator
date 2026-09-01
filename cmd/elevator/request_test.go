package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestDoNotFollowRedirectsStopsAt3xx is a regression test for the actual bug:
// a SigV4-signed request must never be replayed to a redirect target. A
// 307/308 replays the full signed request (Authorization header, session
// token, body) to wherever the redirect points; a 301/302/303 gets
// rewritten to a bodyless GET, whose 200 would otherwise be read here as a
// successful submission even though nothing was actually sent.
func TestDoNotFollowRedirectsStopsAt3xx(t *testing.T) {
	redirectTargetHit := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		redirectTargetHit = true
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
			redirectTargetHit = false
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
			if redirectTargetHit {
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
