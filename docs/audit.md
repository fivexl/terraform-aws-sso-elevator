# Audit

Every grant, revocation, declined request and approved-but-failed request is written to the audit bucket as one JSON object per event, at `<s3_bucket_partition_prefix>/yyyy/MM/dd/<uuid>.json`. The bucket is `s3_name_of_the_existing_bucket` if set, otherwise one the module creates (`sso_elevator_bucket_id` output).

Audit writes never block access expiry. While S3 is unavailable, the requester, revoker and attribute syncer log the full entry to their CloudWatch logs instead, so an Athena table over the bucket misses them. See [accepted risks](accepted-risks.md#an-s3-outage-loses-audit-records-from-the-bucket).

## Fields

The columns below follow `AuditEntry` in `src/s3.py`. Fields that do not apply to an entry hold the string `"NA"`.

`operation_type`:

- `grant`, `revoke`: access given or taken away. `audit_entry_type` says whether it was an account assignment (`account`) or a group membership (`group`).
- `declined`: the request ended without access. `decision_reason` holds a policy reason (such as `NoApprovers`, `NoStatements`, `RequesterNotAllowed` or `NoApproversFoundInSlack`), or `Discarded`, or `Expired`.
- `incomplete`: an approved request failed. `error_message` holds the error, prefixed with the failed step when access was already granted; match it to its `grant` entry by `request_id` or `group_membership_id`. An `incomplete` entry whose `error_message` starts with `granted but grant audit write failed:` has no `grant` entry. The access was granted, and unless the message also names a failed revoke scheduling, it was scheduled for revocation as usual.
- `sync_add`, `sync_remove`, `manual_detected`: [attribute sync](attribute-sync.md) changes. `sync_operation` is `attribute_sync`, and `matched_attributes` and `sso_user_email` are filled.

Other fields worth knowing:

- `request_source` names the request behind the entry: `slack` or `cli` for account requests, and `slack` for every group entry, since group requests come only from Slack. A scheduled revocation holds the source of the request it ends, or `"NA"` if a version before 5.0.0 scheduled it. Entries for access the revoker's sweep removes with no request behind it hold `revoker`, and attribute-sync entries hold `attribute_sync`. `verified_arn` is the STS-verified caller ARN of a CLI request.
- `permission_duration` is in seconds.
- `matched_attributes` is the string `"NA"` when absent but a JSON object when present, so the table declares it `string`; read the populated case with `json_extract`.
- `version` is `2` from 5.0.0 on and null on older records.

## Create the Athena Table

Replace `bucket_name` and `s3_bucket_partition_prefix` (both places), and set the start of `projection.timestamp.range` to the date of your first record.

```
CREATE EXTERNAL TABLE IF NOT EXISTS sso_elevator_table (
  `role_name` string,
  `account_id` string,
  `reason` string,
  `requester_slack_id` string,
  `requester_email` string,
  `request_id` string,
  `approver_slack_id` string,
  `approver_email` string,
  `operation_type` string,
  `permission_duration` string,
  `time` string,
  `group_name` string,
  `group_id` string,
  `group_membership_id` string,
  `audit_entry_type` string,
  `version` string,
  `sso_user_principal_id` string,
  `secondary_domain_was_used` string,
  `sync_operation` string,
  `matched_attributes` string,
  `request_source` string,
  `verified_arn` string,
  `sso_user_email` string,
  `decision_reason` string,
  `error_message` string
)
PARTITIONED BY (`timestamp` string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://bucket_name/s3_bucket_partition_prefix/'
TBLPROPERTIES (
  'projection.enabled'='true',
  'projection.timestamp.format'='yyyy/MM/dd',
  'projection.timestamp.interval'='1',
  'projection.timestamp.interval.unit'='DAYS',
  'projection.timestamp.range'='2023/05/08,NOW',
  'projection.timestamp.type'='date',
  'storage.location.template'='s3://bucket_name/s3_bucket_partition_prefix/${timestamp}/');
```

A table created from the 4.x DDL lacks the two 5.0.0 columns:

```
ALTER TABLE sso_elevator_table ADD COLUMNS (decision_reason string, error_message string)
```

Releases before 2.0.0 could write keys with a double slash (`logs//2024/...`), which the table above does not read. [`fix_path.sh` in the 4.4.3 tree](https://github.com/fivexl/terraform-aws-sso-elevator/blob/4.4.3/athena_query/fix_path.sh) moves such objects to the single-slash path.

## Queries

```
SELECT *
FROM sso_elevator_table
WHERE timestamp >= '2023/05/01' AND timestamp <= '2024/05/12';
```

```
SELECT time, requester_email, account_id, role_name, decision_reason, error_message
FROM sso_elevator_table
WHERE operation_type IN ('declined', 'incomplete')
ORDER BY time DESC;
```
