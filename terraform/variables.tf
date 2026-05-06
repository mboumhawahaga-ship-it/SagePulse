variable "aws_region" {
  description = "AWS region where resources will be created"
  type        = string
  default     = "eu-west-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "ml-cost-optimizer"
}

variable "notification_email" {
  description = "Email address for SNS notifications of cost analysis reports"
  type        = string
  sensitive   = true
}
