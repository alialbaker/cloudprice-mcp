# ── Cedar policy engine ───────────────────────────────────────────────────
#
# Cedar authorises every tool call BEFORE the tool runs. It sits in the
# request path, not in the prompt, so a jailbroken or confused model cannot
# argue its way past it. In the Agent in a Box enforcement taxonomy this is
# a HARD control: the agent has no route around it. Guardrails, by contrast,
# are only conditional - a direct boto3.Converse bypasses them.
#
# Cedar's entity model for an AgentCore gateway:
#   principal  AgentCore::OAuthUser, or the IAM principal
#   action     AgentCore::Action::"<target>___<tool>"
#   resource   AgentCore::Gateway::"<gateway-arn>" - REQUIRED. AgentCore
#              rejects a wildcard resource with "a wildcard resource ..."
#   context    { input: { ...tool arguments... } }
#
# Default posture is DENY. Everything below is an explicit exception.

resource "aws_bedrockagentcore_policy_engine" "cloudprice" {
  name        = replace("${local.name}-policy", "-", "_")
  description = "Authorises tool calls on the cloudprice gateway"
}

locals {
  # Generated from the same file the gateway target uses, so the allow-list
  # cannot drift from the tools that actually exist. Adding a tool means
  # regenerating the schema AND re-applying - which is the point: a new tool
  # is denied until somebody consciously permits it.
  tool_actions = [
    for t in jsondecode(file("${path.module}/../gateway_lambda/tool-schema.json")) :
    "AgentCore::Action::\"cloudpricetools___${t.name}\""
  ]
}

# ── Policy 1: the allow-list ──────────────────────────────────────────────
# Every tool here is a pure read-only function over bundled JSON: no writes,
# no network, no credentials. They are safe to permit as a set. What matters
# is that this is a SET, not a wildcard - tool 26 will not be on it.

resource "aws_bedrockagentcore_policy" "permit_readonly_tools" {
  name             = "permit_readonly_pricing_tools"
  description      = "Permit the read only pricing and FinOps tools on this gateway"
  policy_engine_id = aws_bedrockagentcore_policy_engine.cloudprice.policy_engine_id

  definition {
    cedar {
      statement = <<-CEDAR
        permit(
          principal,
          action in [${join(", ", local.tool_actions)}],
          resource == AgentCore::Gateway::"${aws_bedrockagentcore_gateway.cloudprice.gateway_arn}"
        );
      CEDAR
    }
  }
}

# ── Policy 2: an input guard ──────────────────────────────────────────────
# Demonstrates the property that makes Cedar worth having: the check reads
# the actual tool arguments. A forbid always beats a permit in Cedar, so this
# carves an exception out of the allow-list above.
#
# vcpus in the thousands is not a real sizing question - it is garbage input
# or someone probing. Rejecting it at the gateway means the Lambda is never
# invoked and the model never sees a result to reason about.

resource "aws_bedrockagentcore_policy" "forbid_absurd_sizing" {
  name             = "forbid_absurd_sizing"
  description      = "Reject implausible vcpu counts before the tool runs"
  policy_engine_id = aws_bedrockagentcore_policy_engine.cloudprice.policy_engine_id

  definition {
    cedar {
      statement = <<-CEDAR
        forbid(
          principal,
          action == AgentCore::Action::"cloudpricetools___compare_clouds",
          resource == AgentCore::Gateway::"${aws_bedrockagentcore_gateway.cloudprice.gateway_arn}"
        )
        when {
          context has input &&
          context.input has vcpus &&
          context.input.vcpus > 1024
        };
      CEDAR
    }
  }
}
