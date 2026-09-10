
# Manually create table

Replace bucket_name, partition_prefix and you should be good to go

This DDL previously lagged `src/s3.py`'s `AuditEntry` fields (#194 documentation fix) -- `sync_operation`, `matched_attributes`, `sso_user_email` were already being written and landing in S3 unqueryable, and `request_source`/`verified_arn` (added for the CLI access-request path, to tell a CLI-sourced grant apart from a Slack-sourced one in audit history) were missing entirely. `matched_attributes` is declared `string` here as the least-wrong single type: `s3.py` serializes it as the literal string `"NA"` when absent, but as a nested JSON object of matched attribute names to values when present -- querying the populated case will need `json_extract`/similar rather than a plain column read.

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
  `sso_user_email` string
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