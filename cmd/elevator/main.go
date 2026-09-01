// elevator is a CLI that submits a temporary AWS access request without Slack.
//
// It signs the request directly with the caller's own local AWS credentials
// and sends it to the SSO Elevator API. API Gateway's AWS_IAM authorizer
// verifies that signature itself, so there's nothing left for this CLI to
// do beyond signing and sending — no separate STS call, no header forwarding.
package main

import (
	"fmt"
	"io"
	"os"
)

// version, buildCommit, and buildDate are set via -ldflags -X by
// .goreleaser.yaml at release build time; they stay at these defaults for a
// plain `go build`.
var (
	version     = "dev"
	buildCommit = "none"
	buildDate   = "unknown"
)

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "help", "-h", "--help":
			usage(os.Stdout)
			return
		case "version", "--version", "-V":
			fmt.Printf("elevator %s (commit %s, built %s)\n", version, buildCommit, buildDate)
			return
		case "configure":
			runConfigure(os.Args[2:])
			return
		}
	}
	runRequest(os.Args[1:])
}

// usage is the single source of truth for elevator's help text — reached both
// from `elevator help`/`-h`/`--help` directly, and from either subcommand's
// FlagSet.Usage when its own -h/-help is parsed, so the text is identical no
// matter how it's requested.
func usage(w io.Writer) {
	fmt.Fprint(w, `elevator — submit a temporary AWS access request without Slack.

It signs the request with your local AWS credentials and posts it to the
SSO Elevator API; API Gateway's AWS_IAM authorizer verifies the signature
and the Lambda extracts your identity from it — no separate login step.

Usage:
  elevator --account ID --permission-set NAME --duration HOURS --reason TEXT [flags]
  elevator configure --endpoint URL
  elevator version
  elevator help | -h | --help

Flags (for the default request-submission command):
  --account           AWS account ID to request access to (required)
  --permission-set    Permission set name to request (required)
  --duration          How long access is needed, in hours (required)
  --reason            Reason for the access request (required)
  --endpoint          SSO Elevator API invoke URL — overrides the saved
                      config file and ELEVATOR_ENDPOINT for this call only
  --region            AWS region for SigV4 signing — if omitted, parsed from
                      --endpoint's own hostname when it's a standard
                      execute-api.<region>.amazonaws.com URL, else the
                      resolved AWS config region, falling back to us-east-1

Configuration, in precedence order (highest first):
  1. --endpoint flag
  2. ELEVATOR_ENDPOINT environment variable
  3. ~/.elevator/config.json, written by `+"`elevator configure --endpoint URL`"+`

Credentials and region come from the standard AWS SDK chain — AWS_PROFILE,
AWS_REGION, an active SSO session, etc. Nothing AWS-specific is configured
by this tool directly; set AWS_PROFILE as you would for any AWS CLI command.

Example:
  elevator configure --endpoint https://xxxx.execute-api.us-east-1.amazonaws.com/default/access-requester-cli
  elevator --account 123456789012 --permission-set ReadOnly --duration 2 --reason "debugging prod issue"

What happens after you run it: a successful submission means the request
was received and posted to the approval workflow in Slack — it does NOT
mean access has been granted yet. This command does not wait for or report
the final decision (which may happen automatically, or require someone to
click Approve/Deny in Slack). Check Slack, or the account's IAM Identity
Center assignments, to confirm the actual outcome.
`)
}
