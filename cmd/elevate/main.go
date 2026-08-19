// elevate is a CLI that submits a temporary AWS access request without Slack.
//
// It signs the request directly with the caller's own local AWS credentials
// and sends it to the SSO Elevator API. API Gateway's AWS_IAM authorizer
// verifies that signature itself, so there's nothing left for this CLI to
// do beyond signing and sending — no separate STS call, no header forwarding.
package main

import "os"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "configure" {
		runConfigure(os.Args[2:])
		return
	}
	runRequest(os.Args[1:])
}
