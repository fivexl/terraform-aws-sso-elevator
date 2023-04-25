resource "aws_scheduler_schedule_group" "one_time_schedule_group" {
  name = "sso_elevator_revoke"
  tags = var.tags
}
