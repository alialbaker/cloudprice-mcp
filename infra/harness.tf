# ── AgentCore Harness ─────────────────────────────────────────────────────
#
# A harness is a config-only agent: model + system prompt + tools + memory,
# with AWS running the loop. No container, no code to host. It gives us a
# hosted, invocable agent endpoint - which is what the website will call
# instead of running its own Bedrock loop.
#
# There is no separate charge for the harness itself; you pay only for the
# underlying model tokens and memory.

# ── Execution role ────────────────────────────────────────────────────────

data "aws_iam_policy_document" "harness_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "harness" {
  # Invoking through an application inference profile needs permission on BOTH
  # the profile AND the foundation models it routes to. Granting only the
  # profile fails at runtime. This is the trap that tempts people into
  # bedrock_model_arns = ["*"] - the scoped list below is the correct answer,
  # and it stays scoped because the profile pins the model anyway.
  statement {
    sid    = "InvokeModelViaProfile"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    # Both profiles, so switching model_id between them needs no IAM change.
    # Still exactly two named profiles - not a wildcard over inference-profile/*.
    resources = [
      aws_bedrock_inference_profile.orchestrator.arn,
      aws_bedrock_inference_profile.recommender.arn,
      "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.*",
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
    ]
  }

  # Calling the tool gateway. The gateway then applies Cedar and invokes the
  # tool Lambda under its own role - the harness never touches the Lambda.
  statement {
    sid       = "CallToolGateway"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.cloudprice.gateway_arn]
  }

  # Managed memory. The harness creates the memory resource itself, so its ARN
  # is only known after the harness exists - referencing it here would be a
  # circular ordering dependency (the harness needs this role first). Matched
  # by name pattern instead; AgentCore appends a random suffix.
  statement {
    sid    = "UseManagedMemory"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:DeleteEvent",
      "bedrock-agentcore:GetMemory",
      "bedrock-agentcore:RetrieveMemoryRecords",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:GetMemoryRecord",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:memory/cloudprice_gateway_agent-*",
      "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:memory/cloudprice_gateway_agent-*/*",
    ]
  }
}

resource "aws_iam_role" "harness" {
  name               = "${local.name}-harness"
  description        = "Execution role for the cloudprice harness agent"
  assume_role_policy = data.aws_iam_policy_document.harness_assume.json
}

resource "aws_iam_role_policy" "harness" {
  name   = "${local.name}-harness"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.harness.json
}

# ── The harness ───────────────────────────────────────────────────────────

resource "aws_bedrockagentcore_harness" "cloudprice" {
  harness_name       = replace("${local.name}_agent", "-", "_")
  execution_role_arn = aws_iam_role.harness.arn

  model {
    bedrock_model_config {
      # The tagged application inference profile, never a bare model id -
      # that is what produces per-role cost and usage attribution.
      # Haiku via the recommender profile, not Sonnet: ~12x cheaper per
      # question (~$0.002 vs ~$0.027) because input tokens dominate. This
      # harness is about to be reachable from a public website, so the cheap
      # model is the correct default; use the orchestrator profile for
      # analysis where answer quality justifies the cost.
      model_id    = aws_bedrock_inference_profile.recommender.arn
      temperature = 0
      max_tokens  = 2000
    }
  }

  # Tool authorisation lives in Cedar at the gateway, deliberately NOT here as
  # well. Two places to restrict tools means two places to forget.
  tool {
    type = "agentcore_gateway"
    name = "cloudpricetools"
    config {
      agentcore_gateway {
        gateway_arn = aws_bedrockagentcore_gateway.cloudprice.gateway_arn
        outbound_auth {
          aws_iam = true
        }
      }
    }
  }

  memory {
    managed_memory_configuration {
      # SEMANTIC recalls facts across turns; USER_PREFERENCE remembers stated
      # preferences ("we are an AWS shop", "assume 3-year commitments").
      strategies            = ["SEMANTIC", "USER_PREFERENCE"]
      event_expiry_duration = var.memory_expiry_days
      # Customer-managed key rather than the AWS-managed default, so key use
      # is auditable and the key can be disabled to revoke access to memory.
      encryption_key_arn = aws_kms_key.memory.arn
    }
  }

  system_prompt {
    text = <<-PROMPT
      You are a FinOps assistant for multi-cloud cost decisions across AWS,
      Azure, GCP and OCI.

      Rules:
      - ALWAYS use a tool for any pricing, cost, carbon or sizing question.
        Never state a price from memory - the tools read a maintained catalogue
        and your training data is stale.
      - If a tool call fails, read the error and CALL IT AGAIN with corrected
        arguments. Do not answer from the tool descriptions; they document the
        interface, not the current data.
      - Quote figures exactly as returned, and say which cloud and SKU they
        came from, plus the catalogue as_of date.
      - Surface the honest_gaps a tool returns rather than hiding them.
      - Be concise. Lead with the number, then the reasoning.
    PROMPT
  }

  # Hard cost ceilings, set at creation. An agent loop that will not terminate
  # is the main way a small workload becomes a large bill.
  max_iterations  = var.harness_max_iterations
  max_tokens      = var.harness_max_tokens
  timeout_seconds = 300

  tags = {
    AgentRole = "recommender"
    CostGroup = "cloudprice-agent"
  }
}
