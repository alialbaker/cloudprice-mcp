terraform {
  required_version = ">= 1.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Same state bucket and lock table the portfolio uses, separate key.
  backend "s3" {
    bucket         = "albaker-state-files"
    key            = "cloudprice-gateway/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "albaker-tech-terraform-lock"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "cloudprice-gateway"
      Owner     = "ali"
      ManagedBy = "terraform"
    }
  }
}

locals {
  name = "cloudprice-gateway"
  # Built by: python gateway_lambda/build.py
  zip_path = "${path.module}/../gateway_lambda/dist/cloudprice-gateway.zip"
}

# ── Execution role ────────────────────────────────────────────────────────
# The engine reads bundled JSON and makes no AWS or network calls, so this
# role needs CloudWatch Logs and nothing else. Anything more would be
# permissions the function cannot use.

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-execution"
  description        = "Execution role for the cloudprice AgentCore Gateway target"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ── Log group ─────────────────────────────────────────────────────────────
# Declared explicitly so retention is bounded. Lambda creates this implicitly
# with never-expire retention, which quietly accrues cost forever.

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

# ── The function ──────────────────────────────────────────────────────────

resource "aws_lambda_function" "gateway_target" {
  function_name = local.name
  description   = "cloudprice-mcp tools, exposed to Amazon Bedrock AgentCore Gateway"
  role          = aws_iam_role.lambda.arn

  filename         = local.zip_path
  source_code_hash = filebase64sha256(local.zip_path)

  # Must match how build.py fetched PyYAML's wheel.
  runtime       = "python3.12"
  architectures = ["x86_64"]
  handler       = "handler.lambda_handler"

  # The ~1.8 MB price catalog is parsed once per container on cold start and
  # cached, so memory buys cold-start speed more than steady-state capacity.
  memory_size = var.memory_mb
  timeout     = var.timeout_seconds

  depends_on = [
    aws_iam_role_policy_attachment.logs,
    aws_cloudwatch_log_group.lambda,
  ]
}
