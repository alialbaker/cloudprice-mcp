# ── AgentCore Gateway ─────────────────────────────────────────────────────
#
# Gateway is the front door: it authenticates callers, advertises the tool
# catalogue over MCP, and forwards each tool call to a backend. It does not
# run the tools - the Lambda in main.tf does that.
#
# Authorizer is AWS_IAM deliberately. Only principals in this account can
# call it, which keeps the endpoint private while tool routing is proven.
# Swapping to CUSTOM_JWT with Cognito is a later, separate change.

# ── Tool catalogue in S3 ──────────────────────────────────────────────────
# Gateway sees the Lambda as an opaque ARN, so it has to be told which tools
# live behind it. The schema is generated from the MCP tool definitions by
#   python gateway_lambda/export_tool_schema.py
# and referenced from S3 rather than inlined - 25 tools is ~80 KB, which as
# nested HCL blocks would be thousands of unreadable lines.

resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name}-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_object" "tool_schema" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "tool-schema.json"
  source = "${path.module}/../gateway_lambda/tool-schema.json"
  etag   = filemd5("${path.module}/../gateway_lambda/tool-schema.json")
}

# ── Gateway execution role ────────────────────────────────────────────────
# Gateway assumes this to read the schema and invoke the target. Scoped to
# exactly one object and exactly one function - no wildcards.

data "aws_iam_policy_document" "gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    # No aws:SourceAccount condition. AgentCore does not send that context
    # key, and including it makes the gateway fail to assume this role with
    # "Gateway service is not authorized to perform AssumeRole". Scoping to
    # aws:SourceArn is not possible either: the role must exist before the
    # gateway whose ARN it would name. Blast radius is limited instead by
    # the permissions policy - one Lambda, one S3 object.
  }
}

data "aws_iam_policy_document" "gateway_permissions" {
  statement {
    sid       = "InvokeToolLambda"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.gateway_target.arn]
  }
  statement {
    sid       = "ReadToolSchema"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/${aws_s3_object.tool_schema.key}"]
  }
  # The gateway calls the policy engine on every tool invocation to get an
  # allow/deny decision. AuthorizeAction is the one that matters - the
  # first error message says "GetPolicyEngine", but the actual denied
  # action in the 403 is bedrock-agentcore:AuthorizeAction.
  statement {
    sid    = "AuthorizeAgainstPolicyEngine"
    effect = "Allow"
    # The gateway calls several authorize verbs, not one: AuthorizeAction for
    # a single tool, PartiallyAuthorizeActions when evaluating a set. Granting
    # them individually turned into whack-a-mole as each 403 revealed the next
    # one, so this matches the verb family and relies on the resource scope
    # below to bound it. Worth tightening to an explicit list once AWS
    # documents the full set.
    actions = [
      "bedrock-agentcore:*Authorize*",
      "bedrock-agentcore:GetPolicyEngine",
      "bedrock-agentcore:ListPolicies",
      "bedrock-agentcore:GetPolicy",
    ]
    # AuthorizeAction is evaluated against the GATEWAY, not the engine: the
    # error message names the engine, but the denied resource in the 403 is
    # the gateway ARN.
    #
    # The gateway ARN is built as a pattern rather than referenced, to break
    # a circular ORDERING dependency: referencing the resource would force
    # this policy to apply after the gateway, but the gateway cannot attach
    # a policy engine until this permission already exists. AgentCore appends
    # a random suffix to the gateway name, hence the trailing wildcard - it
    # still confines this to gateways named for this project in this account.
    resources = [
      aws_bedrockagentcore_policy_engine.cloudprice.policy_engine_arn,
      "${aws_bedrockagentcore_policy_engine.cloudprice.policy_engine_arn}/*",
      "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/${local.name}-*",
    ]
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${local.name}-gateway"
  description        = "Execution role for the cloudprice AgentCore Gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
}

resource "aws_iam_role_policy" "gateway" {
  name   = "${local.name}-gateway"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway_permissions.json
}

# ── The gateway ───────────────────────────────────────────────────────────

resource "aws_bedrockagentcore_gateway" "cloudprice" {
  name          = local.name
  description   = "Multi-cloud FinOps tools from cloudprice-mcp, exposed over MCP"
  role_arn      = aws_iam_role.gateway.arn
  protocol_type = "MCP"

  authorizer_type = "AWS_IAM"

  # exception_level is deliberately unset. The only accepted value is DEBUG,
  # which returns internal detail in error responses; the default is safer.

  # LOG_ONLY: Cedar evaluates every call and records the decision, but does
  # not block. Run it this way for a week, read what it WOULD have denied,
  # then flip to ENFORCE. Enforcing an untested policy takes the gateway
  # down and teaches you nothing about why.
  policy_engine_configuration {
    arn  = aws_bedrockagentcore_policy_engine.cloudprice.policy_engine_arn
    mode = var.policy_engine_mode
  }
}

# ── The target ────────────────────────────────────────────────────────────

resource "aws_bedrockagentcore_gateway_target" "cloudprice_tools" {
  gateway_identifier = aws_bedrockagentcore_gateway.cloudprice.gateway_id

  # This name becomes the prefix Gateway puts on every tool, as
  # "cloudpricetools___get_aws_price". handler.py splits on "___".
  name        = "cloudpricetools"
  description = "25 FinOps tools across AWS, Azure, GCP and OCI"

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.gateway_target.arn

        tool_schema {
          s3 {
            uri                     = "s3://${aws_s3_bucket.artifacts.id}/${aws_s3_object.tool_schema.key}"
            bucket_owner_account_id = data.aws_caller_identity.current.account_id
          }
        }
      }
    }
  }

  # Gateway invokes the Lambda as itself, using the role above.
  credential_provider_configuration {
    gateway_iam_role {}
  }
}
