
# Manually create table

Replace bucket_name, partition_prefix and you should be good to go

```
CREATE EXTERNAL TABLE sso_elevator_table_v2 (
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
  `time` string
)
PARTITIONED BY (`timestamp` string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://fivexl-sso-elevator-test-audit-trail-dev/logs/'
TBLPROPERTIES (
  'projection.enabled'='true', 
  'projection.timestamp.format'='yyyy/MM/dd', 
  'projection.timestamp.interval'='1',
  'projection.timestamp.interval.unit'='DAYS', 
  'projection.timestamp.range'='2023/05/08,NOW',	
  'projection.timestamp.type'='date',
  'storage.location.template'='s3://fivexl-sso-elevator-test-audit-trail-dev/logs/${timestamp}/');

```