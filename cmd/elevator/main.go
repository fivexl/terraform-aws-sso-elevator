// elevator is a CLI that submits a temporary AWS access request without Slack.
//
// It signs the request directly with the caller's own local AWS credentials
// and sends it to the SSO Elevator API. API Gateway's AWS_IAM authorizer
// verifies that signature itself, so there's nothing left for this CLI to
// do beyond signing and sending — no separate STS call, no header forwarding.
package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"strings"
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
	// The stdlib log package's default flags prefix every log.Fatal*/
	// log.Print* line with a date and time (#194 D4) -- expected for a
	// long-running service's logs, but unusual and noisy for a one-shot
	// CLI's error output, where the invocation itself already establishes
	// "when".
	log.SetFlags(0)
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

// exitIfHelpRequested prints the same usage text `elevator help`/`-h`/`--help`
// does, to the same stream (stdout), and exits 0 if any of those appear
// anywhere in args -- called by each subcommand, with its own FlagSet (fully
// populated with that subcommand's flags, but not yet given args to parse),
// before that FlagSet.Parse.
//
// Without this, a `-h`/`-help` that isn't the very first argument (e.g.
// `elevator --account 123456789012 --help`, or any `elevator configure -h`)
// never reaches main's own top-level switch at all -- it's parsed by
// FlagSet.Parse itself, whose built-in handling always writes to
// fs.Output(), which defaults to stderr unless a FlagSet explicitly
// overrides it. That produced the same help text on two different streams
// depending on where in the command line -h/-help happened to appear (#194
// D5). Genuine flag *errors* (an unknown flag, a missing required value)
// still go through FlagSet.Parse's own handling on fs.Output() unchanged --
// only usage requested via -h/-help/--help is intercepted here, so an error
// mid-parse still reads as an error, on stderr, not usage text on stdout.
func exitIfHelpRequested(fs *flag.FlagSet, args []string) {
	if helpRequested(fs, args) {
		usage(os.Stdout)
		os.Exit(0)
	}
}

// helpRequested reports whether args contains -h, -help, or --help as a flag
// token in its own right. Split out from exitIfHelpRequested as a pure
// function purely so it's testable without exercising the os.Exit(0) call.
//
// fs is used only to tell a value-taking flag's name from its value: a flag
// like `--reason "--help"` must not be mistaken for a help request just
// because "--help" appears somewhere in args (#194, found live by Andrey
// Devyatkin -- `--reason "--help"` silently opened the help text and exited
// 0 instead of submitting the request, exactly the kind of "reported success
// but nothing happened" failure the D1 fix elsewhere in this CLI exists to
// prevent). This runs before fs.Parse, so it has to do its own lightweight
// walk of args rather than relying on fs.Args()/fs.NArg() -- it mirrors just
// enough of FlagSet.Parse's own token-splitting logic (name[=value] vs a
// separate value argument, and skipping a bool flag's value only when
// spelled with "=") to draw that line correctly.
func helpRequested(fs *flag.FlagSet, args []string) bool {
	skipNext := false
	for _, a := range args {
		if skipNext {
			skipNext = false
			continue
		}
		switch a {
		case "-h", "-help", "--help":
			return true
		}
		name, hasInlineValue := splitFlagToken(a)
		if name == "" || hasInlineValue {
			continue
		}
		f := fs.Lookup(name)
		if f == nil {
			continue
		}
		if bf, ok := f.Value.(interface{ IsBoolFlag() bool }); ok && bf.IsBoolFlag() {
			continue
		}
		skipNext = true
	}
	return false
}

// splitFlagToken reports the flag name a "-name" or "--name[=value]" token
// refers to, and whether it already carries its value via "=". Returns
// name == "" for anything that isn't a flag token at all (doesn't start with
// "-").
func splitFlagToken(a string) (name string, hasInlineValue bool) {
	if len(a) < 2 || a[0] != '-' {
		return "", false
	}
	name = strings.TrimLeft(a, "-")
	if eq := strings.IndexByte(name, '='); eq >= 0 {
		return name[:eq], true
	}
	return name, false
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
  elevator --account ID --permission-set NAME --duration MINUTES --reason TEXT [flags]
  elevator configure --endpoint URL
  elevator version
  elevator help | -h | --help

Flags (for the default request-submission command):
  --account           AWS account ID to request access to (required)
  --permission-set    Permission set name to request (required)
  --duration          How long access is needed, in minutes (required) —
                      any positive whole number, not limited to the specific
                      options the Slack request modal's dropdown shows
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
  elevator --account 123456789012 --permission-set ReadOnly --duration 120 --reason "debugging prod issue"

What happens after you run it: a successful submission means the request
was received and posted to the approval workflow in Slack — it does NOT
mean access has been granted yet. This command does not wait for or report
the final decision (which may happen automatically, or require someone to
click Approve/Deny in Slack). Check Slack, or the account's IAM Identity
Center assignments, to confirm the actual outcome.
`)
}
