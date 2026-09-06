# ── Security hardening ────────────────────────────────────────────────────
# Follows the patterns in the AWS samples repo,
# 01-features/04-manage-context-of-your-agent/memory/05-security/:
#   - customer-managed KMS key on the memory resource
#   - CloudTrail so bedrock-agentcore:actorId and kms:Decrypt are auditable
#
# Not yet implemented from that guidance: an IAM StringEquals condition on
# bedrock-agentcore:actorId. That requires per-user credentials (Cognito
# federated identity, their pattern 02) rather than one shared role. Until
# then the caller MUST derive actorId server-side from a signed session and
# never from anything the browser supplies.

# ── Customer-managed key for memory ───────────────────────────────────────

resource "aws_kms_key" "memory" {
  description             = "Encrypts AgentCore memory for the cloudprice agent"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "memory" {
  name          = "alias/${local.name}-memory"
  target_key_id = aws_kms_key.memory.key_id
}

data "aws_iam_policy_document" "memory_key" {
  # Without this the key is unmanageable - lock yourself out and only AWS
  # support can help.
  statement {
    sid       = "AccountRoot"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  # AgentCore encrypts and decrypts memory content with this key. Scoped by
  # SourceAccount so another account's AgentCore cannot use it.
  statement {
    sid    = "AgentCoreUseOfKey"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_kms_key_policy" "memory" {
  key_id = aws_kms_key.memory.id
  policy = data.aws_iam_policy_document.memory_key.json
}

# The harness role must be able to use the key, or every memory read fails.
data "aws_iam_policy_document" "harness_kms" {
  statement {
    sid    = "UseMemoryKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.memory.arn]
  }
}

resource "aws_iam_role_policy" "harness_kms" {
  name   = "${local.name}-harness-kms"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.harness_kms.json
}

# ── CloudTrail ────────────────────────────────────────────────────────────
# There was no trail in this account at all. Management events for a single
# trail are free; the only cost is S3 storage, which is pennies at this volume.
#
# Data events (which carry bedrock-agentcore:actorId on each memory call) are
# billed per event and are deliberately NOT enabled here. Turn them on when
# there is a real tenant to audit.

resource "aws_s3_bucket" "trail" {
  bucket        = "${local.name}-cloudtrail-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Audit logs are only useful if they cannot be quietly edited.
resource "aws_s3_bucket_versioning" "trail" {
  bucket = aws_s3_bucket.trail.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    id     = "expire-old-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = var.trail_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "AWSCloudTrailAclCheck"
    effect    = "Allow"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }
  statement {
    sid       = "AWSCloudTrailWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json
}

resource "aws_cloudtrail" "account" {
  name           = local.name
  s3_bucket_name = aws_s3_bucket.trail.id

  is_multi_region_trail         = true
  include_global_service_events = true
  # Detects tampering with the log files themselves.
  enable_log_file_validation = true

  depends_on = [aws_s3_bucket_policy.trail]
}
