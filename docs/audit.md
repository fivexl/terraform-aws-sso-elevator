# Audit

## Manually create table

Replace bucket_name, partition_prefix and you should be good to go

This DDL follows `src/s3.py`'s `AuditEntry` fields: `sync_operation`, `matched_attributes`, and `sso_user_email` come from the attribute-sync path, and `request_source`/`verified_arn` let a CLI-sourced grant be told apart from a Slack-sourced one in audit history. `matched_attributes` is declared `string` here as the least-wrong single type: `s3.py` serializes it as the literal string `"NA"` when absent, but as a nested JSON object of matched attribute names to values when present -- querying the populated case will need `json_extract`/similar rather than a plain column read.

Besides `grant` and `revoke`, `operation_type` can be `declined` (the request ended without access: `decision_reason` holds a policy reason such as `NoApprovers`, or `Discarded`, or `Expired`) or `incomplete` (an approved request failed; `error_message` holds the error, prefixed with the failed step when access was already granted -- match it to its `grant` entry by `request_id` or `group_membership_id`). An `incomplete` entry whose `error_message` starts with `granted but grant audit write failed:` has no `grant` entry: the access was granted, and unless the message also names a failed revoke scheduling, it was scheduled for revocation as usual. While S3 is unavailable, entries are written to the Lambda's CloudWatch logs instead, so this table misses them. `version` is `2` from this schema on and null on older records.

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

To add the new columns to a table created from an earlier version of this DDL:

```
ALTER TABLE sso_elevator_table ADD COLUMNS (decision_reason string, error_message string)
```

## Query by date
```
SELECT *
FROM sso_elevator_table
WHERE timestamp >= '2023/05/01' AND timestamp <= '2024/05/12';
```
## Query everything
```
SELECT *
FROM sso_elevator_table;

```
