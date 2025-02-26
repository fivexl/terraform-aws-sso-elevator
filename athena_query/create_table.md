
# Manually create table

Replace bucket_name, partition_prefix and you should be good to go

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
  `secondary_domain_was_used` string
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