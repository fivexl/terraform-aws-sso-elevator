resource "aws_scheduler_schedule_group" "one_time_schedule_group" {
  name = local.schedule_group_name
  tags = var.tags
}
