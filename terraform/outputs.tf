output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.cost_analyzer.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.cost_analyzer.arn
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket for reports"
  value       = aws_s3_bucket.cost_reports.id
}

output "s3_bucket_arn" {
  description = "ARN of the S3 bucket"
  value       = aws_s3_bucket.cost_reports.arn
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic for cost analysis notifications"
  value       = aws_sns_topic.cost_analysis_notifications.arn
}

output "eventbridge_rule_name" {
  description = "Name of the EventBridge rule"
  value       = aws_cloudwatch_event_rule.ml_cost_analysis_schedule.name
}
