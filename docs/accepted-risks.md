# Accepted risks

Known weaknesses we chose not to fix, and why. Report anything not listed here as described in
[SECURITY.md](../SECURITY.md).

## Open

### WAF Common rule set blocks some request reasons

- **What.** With `waf_enabled = true`, `AWSManagedRulesCommonRuleSet` blocks requests whose body
  matches its patterns. A reason containing markup such as `<script>` matches
  `CrossSiteScripting_BODY`. Slack shows the user a generic error; the CLI gets `403`. Other
  Slack payloads may match too.
- **Why accepted.** The rule set is the standard baseline for public endpoints. A real reason
  rarely contains markup, and per-field exceptions inside a managed rule group add rules to
  maintain for little gain.
- **Mitigation.** The user rephrases the reason. The WAF log names the rule that blocked the
  request. If a rule blocks real traffic, set it to Count in a web ACL you manage and attach that
  with `waf_web_acl_arn` instead.

### WAF inspects only the first 16 KB of a body

- **What.** API Gateway passes WAF the first 16 KB of a request body, and the module counts
  `SizeRestrictions_BODY` instead of blocking it. Content after 16 KB is not inspected.
- **Why accepted.** Slack modal submissions can exceed the rule's 8 KB limit, so blocking on size
  would break Slack. A larger inspection limit costs extra per request.
- **Mitigation.** Every Slack request must carry a valid Slack signature, which the Lambda
  verifies. The Lambda rejects CLI bodies over 64 KiB and CLI payloads over 16 KiB.

### The Slack header check tests presence, not validity

- **What.** The API Gateway request validator rejects a Slack-route POST only when
  `X-Slack-Signature` or `X-Slack-Request-Timestamp` is missing. A request with made-up values
  still reaches the Lambda and can cold-start it.
- **Why accepted.** API Gateway cannot check an HMAC. The validator removes the cheapest case,
  an empty POST, without needing WAF.
- **Mitigation.** The Lambda verifies the signature and timestamp and rejects the request.
  API Gateway throttling and the optional WAF rate limit bound the volume.

### A CLI proof can be replayed for about 90 seconds

- **What.** The Lambda accepts a CLI identity proof up to 60 seconds after it was signed, and up
  to 30 seconds ahead for clock skew. Within that window, someone holding the exact request body
  can send it again. The replay repeats the identical request: account, permission set, duration
  and reason cannot change. It can post a duplicate approval message and audit entry, and an
  auto-approved request can be granted again with a later expiry. The nonce in the proof is
  random but not stored, so it does not stop a replay. STS itself accepts a presigned
  GetCallerIdentity for about 15 minutes regardless of `X-Amz-Expires`, so a leaked body is a
  bearer token at any third-party GetCallerIdentity verifier that does not require its own
  signed audience header.
- **Why accepted.** Stopping replays needs a nonce store, a stateful resource, against an attack
  that needs the exact body from inside a TLS session and only repeats the caller's own request.
- **Mitigation.** The short window. The body travels only over TLS, and the Lambda never logs the
  proof. Sending the replay through API Gateway also needs a valid request signature from an
  organization principal.

### Async retries are off only for function errors

- **What.** The `live` alias has `maximum_retry_attempts = 0`, so Lambda does not retry a Slack
  lazy listener (approve, deny) after a function error. Lambda still redelivers an async event
  after throttling or a Lambda system error, which can repeat its Slack posts or grant.
- **Why accepted.** Lambda offers no setting that turns off those redeliveries.
- **Mitigation.** Redelivery needs the function to be throttled or Lambda to fail internally.
  Watch the function's `Throttles` metric.

### The 5.0.0 upgrade has downtime

- **What.** The apply that upgrades to 5.0.0 destroys the old API Gateway APIs and creates a new
  one with new URLs. Slack and the CLI fail until the Slack Request URL and CLI configuration are
  updated.
- **Why accepted.** Running both APIs side by side for a release would keep the HTTP API, which
  supports neither WAF nor resource policies, alive longer.
- **Mitigation.** The README's "Upgrade to 5.0.0" lists the steps to run right after the apply.

### Published 4.x versions keep the forgeable code

- **What.** Each published Lambda version keeps its own code and stays invocable by version
  number. Versions from 4.x with the CLI route on still trust the identity in the event, so
  anyone allowed `lambda:InvokeFunction` on them can forge a CLI request for another user.
- **Why accepted.** Terraform does not delete previously published versions, so the module cannot
  remove them during the upgrade.
- **Mitigation.** The README's "Upgrade to 5.0.0" has the step that deletes them.

### An S3 outage loses audit records from the bucket

- **What.** Audit writes fail open. When S3 fails or is slow, grants and revocations go ahead
  without their S3 audit record, so Athena queries miss them. A reconciliation sweep stops
  trying S3 after its first failed or slow (over 1 s) write.
- **Why accepted.** Blocking grants or expiry on S3 would turn an S3 incident into an access
  incident: grants stop, or access outlives its expiry.
- **Mitigation.** Each lost entry is written in full to the Lambda's CloudWatch logs, which are
  then the record. A grant whose audit write failed shows a warning in its Slack thread and, when
  S3 accepts it, an `incomplete` entry.

### Access outlives its expiry when both audit and scheduling fail

- **What.** If the grant audit write and the revocation schedule both fail, nothing revokes the
  access at its end time. It lasts until the next reconciliation sweep (daily by default).
- **Why accepted.** Rolling the grant back can fail too, and for a group it would remove a
  membership the user may have had before the request.
- **Mitigation.** Slack marks the request as not scheduled and says the inconsistency check will
  remove it. Revoke it manually for a faster end.

## Closed

### Direct Lambda invoke could forge a CLI identity (fixed in 5.0.0)

Before 5.0.0 the Lambda took the CLI caller's identity from the API Gateway event, so anyone
allowed `lambda:InvokeFunction` on it could invoke it directly with an event naming another
user. The CLI now sends a presigned `sts:GetCallerIdentity` proof, and the Lambda takes the
identity from STS's answer.
