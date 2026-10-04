# API Gateway

The requester Lambda sits behind one REST API (`api_gateway_name`, stage `default`) with two routes, both invoking the Lambda's `live` alias:

- `POST /access-requester` for Slack (`requester_api_endpoint_url` output). Open to the internet; the Lambda verifies Slack's request signature. A request validator answers `400` without invoking the Lambda when the `X-Slack-Signature` or `X-Slack-Request-Timestamp` header is missing, so unsigned POSTs cannot cold-start it. The validator checks that the headers are present, not that they are valid ([accepted risks](accepted-risks.md#the-slack-header-check-tests-presence-not-validity)).
- `POST /access-requester-cli` for the [CLI](cli.md) (`requester_api_endpoint_url_cli` output), unless `enable_access_requester_cli = false`. Uses `AWS_IAM` authorization, and the resource policy admits only principals in this AWS Organization.

It is a REST API rather than an HTTP API because only REST APIs support WAF, resource policies and request validation.

Each route is throttled separately with `api_gateway_throttling_burst_limit` and `api_gateway_throttling_rate_limit`.

Each apply that changes the Lambda publishes a new version and moves `live` to it. Async invokes of `live` (Slack lazy listeners, such as approve and deny) are not retried after a function error, though Lambda still redelivers them after throttling or a Lambda system error ([accepted risks](accepted-risks.md#async-retries-are-off-only-for-function-errors)).

## Access logs

Stage access logs are off by default. `api_gateway_access_logs_enabled = true` creates the log group `/aws/apigateway/<api_gateway_name>/default/access` (retention `logs_retention_in_days`) and turns them on. REST API logging needs the account-wide API Gateway CloudWatch Logs role (`aws_api_gateway_account`) to be set already, or the apply fails. The module does not set it, because other APIs in the account may depend on its current value.

## SnapStart

Slack drops a request the Lambda hasn't answered within 3 seconds, and a cold start of the requester spends most of that on imports, the approval config from S3, both Slack secrets and Slack's `auth.test`. With `snap_start = true` (the default) Lambda takes a snapshot of the initialized requester when Terraform publishes a version, and new execution environments start from it.

Set `snap_start = false` where Lambda does not offer SnapStart for the runtime and package type in your region: the apply fails there. The pre-built images are container images; see [Lambda images](deployment.md#lambda-images) for where they exist.

The snapshot holds no approval rules and no Slack secrets. A SnapStart restore hook reads them after each restore, so a restored environment is never staler than a cold-started one. The hook's reads time out within seconds to fit Lambda's restore timeout; if one fails, Lambda fails the restore and the request errors rather than run with the snapshot's empty rules.

Lambda bills SnapStart for Python per cached version and per restore. Each apply that changes the requester publishes a version, so the revoker's nightly run (`schedule_expression`) deletes all but the version `live` points to and one Active version below it, kept for rollback. Versions above `live` are left alone, since one may be mid-publish. Pruning runs whatever `snap_start` is set to.

With `snap_start = false` the requester reads everything at cold start.

## AWS WAF

Optional, off by default. Two modes, which cannot be combined:

- `waf_enabled = true`: the module creates a REGIONAL web ACL, associates it with the API stage and logs to the CloudWatch log group `aws-waf-logs-<api_gateway_name>`, with the `authorization`, `x-amz-security-token` and `x-slack-signature` headers redacted. CloudWatch metrics are on; request sampling is off, because redaction does not apply to sampled requests. Rules, in order:
  1. A per-IP rate limit, `waf_rate_limit` requests per 5 minutes (default 1000, minimum 10). It stops one noisy IP from using up the API Gateway throttle that all callers share. All Slack traffic arrives from Slack's shared IPs and one access request takes about 5 calls, so a low limit blocks Slack bursts; the API Gateway throttle stays the tighter overall cap.
  2. `AWSManagedRulesCommonRuleSet`, with `SizeRestrictions_BODY` set to Count because Slack modal submissions can exceed its 8 KB limit.
  3. `AWSManagedRulesKnownBadInputsRuleSet`.
  4. `AWSManagedRulesAmazonIpReputationList`.

  Cost is about $9 a month plus $0.60 per million requests.
- `waf_web_acl_arn = "<arn>"`: associates a REGIONAL web ACL you manage.

Setting both fails at plan when the ARN is known then; an ARN of a web ACL created in the same apply defers that check to apply time.

If AWS Firewall Manager associates a web ACL with your API Gateway stages, leave both unset: an association from the module would conflict with it.

The Common rule set can block a legitimate request whose reason contains markup such as `<script>`, and WAF inspects only the first 16 KB of a body. See [accepted risks](accepted-risks.md#waf-common-rule-set-blocks-some-request-reasons).

The `waf_web_acl_arn` output is the associated web ACL in either mode.
