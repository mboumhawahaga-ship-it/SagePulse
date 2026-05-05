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

output "dynamodb_audit_table_name" {
  description = "Name of the DynamoDB audit trail table"
  value       = aws_dynamodb_table.audit_trail.name
}
