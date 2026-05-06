terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # S3 backend — state stocké hors du repo, verrouillage via DynamoDB
  # Créer le bucket et la table manuellement une seule fois avant terraform init :
  #   aws s3 mb s3://ml-cost-optimizer-tfstate --region eu-west-1
  #   aws s3api put-bucket-versioning --bucket ml-cost-optimizer-tfstate --versioning-configuration Status=Enabled
  #   aws dynamodb create-table --table-name ml-cost-optimizer-tflock \
  #     --attribute-definitions AttributeName=LockID,AttributeType=S \
  #     --key-schema AttributeName=LockID,KeyType=HASH \
  #     --billing-mode PAY_PER_REQUEST --region eu-west-1
  backend "s3" {
    bucket         = "ml-cost-optimizer-tfstate"
    key            = "ml-cost-optimizer/terraform.tfstate"
    region         = "eu-west-1"
    encrypt        = true
    dynamodb_table = "ml-cost-optimizer-tflock"
  }
}

provider "aws" {
  region = var.aws_region
}

# Generer un ID unique pour eviter les conflits de noms
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# S3 Bucket pour stocker les rapports
resource "aws_s3_bucket" "cost_reports" {
  bucket = "${var.project_name}-reports-${random_id.bucket_suffix.hex}"

  tags = {
    Name        = "ML Cost Reports"
    Project     = var.project_name
    ManagedBy   = "Terraform"
  }
}

# SNS Topic pour les notifications
resource "aws_sns_topic" "cost_analysis_notifications" {
  name = "${var.project_name}-notifications"

  tags = {
    Project   = var.project_name
    ManagedBy = "Terraform"
  }
}

# SNS Topic Subscription - Subscribe email
resource "aws_sns_topic_subscription" "cost_analysis_email" {
  topic_arn = aws_sns_topic.cost_analysis_notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# IAM Role pour Lambda
resource "aws_iam_role" "lambda_role" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project   = var.project_name
    ManagedBy = "Terraform"
  }
}

# IAM Policy pour Lambda - Logs (common to all Lambda functions)
resource "aws_iam_role_policy" "lambda_logs_policy" {
  name = "${var.project_name}-lambda-logs-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# Lambda Function
resource "aws_lambda_function" "cost_analyzer" {
  filename         = "../lambda/function.zip"
  function_name    = "${var.project_name}-analyzer"
  role            = aws_iam_role.lambda_role.arn
  handler         = "main.handler"
  source_code_hash = filebase64sha256("../lambda/function.zip")
  runtime         = "python3.12"
  timeout         = 60
  memory_size     = 256

  environment {
    variables = {
      REPORT_BUCKET = aws_s3_bucket.cost_reports.id
      PROJECT_NAME  = var.project_name
      SNS_TOPIC_ARN = aws_sns_topic.cost_analysis_notifications.arn
    }
  }

  tags = {
    Project   = var.project_name
    ManagedBy = "Terraform"
  }
}

# CloudWatch Log Group pour Lambda cost_analyzer
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${aws_lambda_function.cost_analyzer.function_name}"
  retention_in_days = 7

  tags = {
    Project   = var.project_name
    ManagedBy = "Terraform"
  }
}

# Lambda Function - Action (exécutée après approbation humaine)
resource "aws_lambda_function" "cost_action" {
  filename         = "../lambda/function.zip"
  function_name    = "${var.project_name}-action"
  role             = aws_iam_role.lambda_role.arn
  handler          = "action.handler"
  source_code_hash = filebase64sha256("../lambda/function.zip")
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      PROJECT_NAME = var.project_name
    }
  }

  tags = {
    Project   = var.project_name
    ManagedBy = "Terraform"
  }
}

# CloudWatch Log Group pour Lambda cost_action
resource "aws_cloudwatch_log_group" "lambda_action_logs" {
  name              = "/aws/lambda/${aws_lambda_function.cost_action.function_name}"
  retention_in_days = 7

  tags = {
    Project   = var.project_name
    ManagedBy = "Terraform"
  }
}

# S3 - Bloquer tout accès public
resource "aws_s3_bucket_public_access_block" "cost_reports" {
  bucket = aws_s3_bucket.cost_reports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# S3 - Versioning des rapports
resource "aws_s3_bucket_versioning" "cost_reports" {
  bucket = aws_s3_bucket.cost_reports.id

  versioning_configuration {
    status = "Enabled"
  }
}
