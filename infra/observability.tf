# ── Use case 2 for inference profiles: usage visibility ───────────────────
#
# Tags on an inference profile answer "what did this cost" via Cost Explorer,
# but that data lags 8-12 hours. The second use case is operational: AWS/Bedrock
# metrics are dimensioned by ModelId, and when a request is made *through* an
# application inference profile, ModelId carries the profile instead of the raw
# model. That turns the profile into a per-agent-role metric dimension.
#
# The account-wide alarms in the portfolio repo cannot distinguish the agent
# from the portfolio chatbot. These can.

# ── Model invocation logging ──────────────────────────────────────────────
# Off by default. Once on, every invocation is logged with its prompt and
# completion, which is what makes the invocation log table useful.
#
# NOTE ON CONTENT: this logs model input and output. Harmless for pricing
# questions. On a workload carrying regulated data it is a classification
# decision, not a config toggle - and the destination log group must be
# encrypted with a CMK and given real retention before enabling it.

resource "aws_cloudwatch_log_group" "bedrock_invocations" {
  name              = "/aws/bedrock/model-invocations"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "bedrock_logging_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_iam_policy_document" "bedrock_logging" {
  statement {
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.bedrock_invocations.arn}:*"]
  }
}

resource "aws_iam_role" "bedrock_logging" {
  name               = "${local.name}-bedrock-logging"
  description        = "Lets Bedrock write model invocation logs"
  assume_role_policy = data.aws_iam_policy_document.bedrock_logging_assume.json
}

resource "aws_iam_role_policy" "bedrock_logging" {
  name   = "${local.name}-bedrock-logging"
  role   = aws_iam_role.bedrock_logging.id
  policy = data.aws_iam_policy_document.bedrock_logging.json
}

resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  logging_config {
    text_data_delivery_enabled      = true
    image_data_delivery_enabled     = false
    embedding_data_delivery_enabled = false
    video_data_delivery_enabled     = false

    cloudwatch_config {
      log_group_name = aws_cloudwatch_log_group.bedrock_invocations.name
      role_arn       = aws_iam_role.bedrock_logging.arn
    }
  }

  depends_on = [aws_iam_role_policy.bedrock_logging]
}

# ── Per-profile alarms ────────────────────────────────────────────────────
# Scoped by ModelId so a spike names the role that caused it.
#
# These sit in INSUFFICIENT_DATA until something actually invokes through a
# profile. That is correct, not broken: treat_missing_data is notBreaching,
# because a quiet hour is normal and alarming on it teaches you to ignore
# the alerts.

locals {
  # The ModelId dimension carries the inference profile ID, NOT its ARN.
  # Verified empirically: after invoking through the orchestrator profile,
  # AWS/Bedrock reported ModelId="9kxwed2urtar". Alarms keyed on the ARN
  # match nothing and sit silent forever - a failure with no symptom.
  agent_profiles = {
    orchestrator = aws_bedrock_inference_profile.orchestrator.id
    recommender  = aws_bedrock_inference_profile.recommender.id
  }

  # USD per million tokens, used only to turn token counts into a number a
  # human reacts to. Hardcoded because CloudWatch metric math cannot look a
  # price up - which is a little ironic in this repo. Update when AWS moves
  # rates; being 20% stale is still far better than reading raw token counts
  # and guessing.
  token_rates = {
    orchestrator = { in = 3.00, out = 15.00 } # Sonnet 4.5
    recommender  = { in = 0.25, out = 1.25 }  # Haiku
  }
}

resource "aws_cloudwatch_metric_alarm" "profile_token_spike" {
  for_each = local.agent_profiles

  alarm_name        = "${local.name}-${each.key}-token-spike"
  alarm_description = "Input tokens through the ${each.key} inference profile exceeded the hourly ceiling"

  namespace   = "AWS/Bedrock"
  metric_name = "InputTokenCount"
  statistic   = "Sum"
  dimensions  = { ModelId = each.value }

  period              = 3600
  evaluation_periods  = 1
  threshold           = var.profile_token_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "profile_throttles" {
  for_each = local.agent_profiles

  alarm_name        = "${local.name}-${each.key}-throttles"
  alarm_description = "The ${each.key} profile is being throttled - the agent is retrying and latency is climbing"

  namespace   = "AWS/Bedrock"
  metric_name = "InvocationThrottles"
  statistic   = "Sum"
  dimensions  = { ModelId = each.value }

  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# Own topic rather than reaching into the portfolio's state for its ARN.
resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ── Dashboard ─────────────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "agent" {
  dashboard_name = local.name

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "text", x = 0, y = 0, width = 24, height = 2
        properties = {
          markdown = "# cloudprice agent\nUsage split by Bedrock **application inference profile**. Empty panels mean nothing has invoked through a profile yet - the Gateway and Lambda are measured separately below."
        }
      },
      {
        type = "metric", x = 0, y = 2, width = 12, height = 6
        properties = {
          title  = "Tokens by agent role"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          # CloudWatch requires each metric to be its own array of strings,
          # with an optional options object last. Do not flatten these.
          metrics = concat(
            [for role, arn in local.agent_profiles :
            ["AWS/Bedrock", "InputTokenCount", "ModelId", arn, { label = "${role} in" }]],
            [for role, arn in local.agent_profiles :
            ["AWS/Bedrock", "OutputTokenCount", "ModelId", arn, { label = "${role} out" }]],
          )
        }
      },
      {
        type = "metric", x = 12, y = 2, width = 12, height = 6
        properties = {
          title  = "Invocations and throttles by role"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = concat(
            [for role, arn in local.agent_profiles :
            ["AWS/Bedrock", "Invocations", "ModelId", arn, { label = "${role} calls" }]],
            [for role, arn in local.agent_profiles :
            ["AWS/Bedrock", "InvocationThrottles", "ModelId", arn, { label = "${role} throttled" }]],
          )
        }
      },
      {
        type = "metric", x = 0, y = 8, width = 24, height = 6
        properties = {
          title  = "Gateway - MCP calls and latency"
          region = var.region
          view   = "timeSeries"
          period = 300
          # AWS/Bedrock-AgentCore dimensions this by Operation/Method/Protocol,
          # so tools/list and tools/call are separable. These populate as soon
          # as anything speaks MCP to the gateway.
          metrics = [
            ["AWS/Bedrock-AgentCore", "Invocations", "Operation", "InvokeGateway", "Method", "tools/call", "Protocol", "MCP", { stat = "Sum", label = "tools/call" }],
            ["...", "tools/list", ".", ".", { stat = "Sum", label = "tools/list" }],
            ["AWS/Bedrock-AgentCore", "Latency", "Operation", "InvokeGateway", "Method", "tools/call", "Protocol", "MCP", { stat = "Average", label = "tools/call latency", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 14, width = 12, height = 6
        properties = {
          title  = "Tool Lambda - invocations, errors, duration"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.gateway_target.function_name],
            [".", "Errors", ".", "."],
            [".", "Throttles", ".", "."],
            [".", "Duration", ".", ".", { stat = "Average", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 20, width = 12, height = 6
        properties = {
          title  = "Harness - invocations, errors, throttles"
          region = var.region
          view   = "timeSeries"
          period = 300
          # SEARCH handles the EndpointQualifier/HarnessId/Operation dimension
          # combination without enumerating every permutation. Each entry must
          # be its own array - CloudWatch rejects a flat list of objects.
          metrics = [
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,EndpointQualifier,HarnessId,Operation} HarnessId=\"${aws_bedrockagentcore_harness.cloudprice.harness_id}\" MetricName=\"Invocations\"', 'Sum', 300))", label = "invocations", id = "hi" }],
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,EndpointQualifier,HarnessId,Operation} HarnessId=\"${aws_bedrockagentcore_harness.cloudprice.harness_id}\" MetricName=\"Errors\"', 'Sum', 300))", label = "errors", id = "he" }],
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,EndpointQualifier,HarnessId,Operation} HarnessId=\"${aws_bedrockagentcore_harness.cloudprice.harness_id}\" MetricName=\"Throttles\"', 'Sum', 300))", label = "throttles", id = "ht" }],
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,EndpointQualifier,HarnessId,Operation} HarnessId=\"${aws_bedrockagentcore_harness.cloudprice.harness_id}\" MetricName=\"Latency\"', 'Average', 300))", label = "latency ms", id = "hl", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 20, width = 12, height = 6
        properties = {
          title  = "Memory - events written, records extracted, errors"
          region = var.region
          view   = "timeSeries"
          period = 300
          # Memory errors here are usually IAM or KMS problems on the memory
          # path, which is exactly what the CMK makes possible to break.
          metrics = [
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,ItemType,Resource} MetricName=\"CreationCount\"', 'Sum', 300))", label = "events + records", id = "mc" }],
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,Operation} MetricName=\"Errors\"', 'Sum', 300))", label = "errors", id = "me" }],
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,Operation} MetricName=\"UserErrors\"', 'Sum', 300))", label = "user errors", id = "mu" }],
          ]
        }
      },
      {
        type = "log", x = 12, y = 14, width = 12, height = 6
        properties = {
          title  = "Tool errors"
          region = var.region
          query  = "SOURCE '${aws_cloudwatch_log_group.lambda.name}' | fields @timestamp, @message | filter @message like /error/ | sort @timestamp desc | limit 20"
        }
      },

      # ── Governance and cost ───────────────────────────────────────────────
      {
        type = "text", x = 0, y = 26, width = 24, height = 2
        properties = {
          markdown = "## Governance and cost\nThe panels above say the system is *running*. These say it is **governed** and what it **costs**. The left panel is the one to read first: if the ENFORCE line is flat at zero while tools are being called, the policy engine is not actually deciding anything."
        }
      },
      {
        type = "metric", x = 0, y = 28, width = 8, height = 6
        properties = {
          title  = "Policy decisions by mode"
          region = var.region
          view   = "timeSeries"
          period = 300
          # AllowDecisions carries a Mode dimension, which makes enforcement
          # observable rather than assumed. A policy engine can sit in
          # LOG_ONLY for months while everyone believes it is enforcing;
          # this is the panel that catches that, and it needs no extra
          # instrumentation because AgentCore already emits it.
          metrics = [
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,TargetResource,ToolName,OperationName,Mode} Mode=\"ENFORCE\" MetricName=\"AllowDecisions\"', 'Sum', 300))", label = "allowed (ENFORCE)", id = "pe" }],
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,TargetResource,ToolName,OperationName,Mode} Mode=\"LOG_ONLY\" MetricName=\"AllowDecisions\"', 'Sum', 300))", label = "allowed (LOG_ONLY - not enforcing)", id = "pl" }],
          ]
        }
      },
      {
        type = "metric", x = 8, y = 28, width = 8, height = 6
        properties = {
          title  = "Policy evaluation anomalies"
          region = var.region
          view   = "timeSeries"
          period = 300
          # Under ENFORCE a request whose actions match no permit is refused
          # before the tool runs. A sustained rise here is one of two things
          # and both are worth a look: the allow-list has drifted from the
          # tools that exist, or somebody is probing for tools they should
          # not reach.
          metrics = [
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,TargetResource,OperationName,PolicyEngine} MetricName=\"TotalMismatchedPolicies\"', 'Sum', 300))", label = "mismatched policies", id = "pm" }],
            [{ expression = "SUM(SEARCH('{AWS/Bedrock-AgentCore,OperationName} MetricName=\"DeterminingPolicies\"', 'Sum', 300))", label = "determining policies", id = "pd" }],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 28, width = 8, height = 6
        properties = {
          title   = "Estimated model spend (USD)"
          region  = var.region
          view    = "timeSeries"
          stacked = true
          period  = 3600
          # Token counts are not a number anyone reacts to. Dollars are.
          # Raw counts stay hidden so the panel reads as money.
          metrics = concat(
            [for role, id in local.agent_profiles :
            ["AWS/Bedrock", "InputTokenCount", "ModelId", id, { id = "${role}i", stat = "Sum", visible = false }]],
            [for role, id in local.agent_profiles :
            ["AWS/Bedrock", "OutputTokenCount", "ModelId", id, { id = "${role}o", stat = "Sum", visible = false }]],
            [for role, rate in local.token_rates :
            [{ expression = "(${role}i / 1000000 * ${rate.in}) + (${role}o / 1000000 * ${rate.out})", label = "${role}", id = "${role}c" }]],
          )
        }
      },
      {
        type = "alarm", x = 0, y = 34, width = 24, height = 4
        properties = {
          title = "Alarm state - INSUFFICIENT_DATA is not the same as healthy"
          # An alarm keyed to a dimension value that never occurs sits in
          # INSUFFICIENT_DATA forever: it never fires, never errors, and looks
          # fine on every other panel. Putting the states on the dashboard is
          # the cheapest way to notice. Ordered most-severe first.
          sortBy = "stateUpdatedTimestamp"
          states = ["ALARM", "INSUFFICIENT_DATA", "OK"]
          alarms = concat(
            [for a in aws_cloudwatch_metric_alarm.profile_token_spike : a.arn],
            [for a in aws_cloudwatch_metric_alarm.profile_throttles : a.arn],
            [aws_cloudwatch_metric_alarm.policy_mismatch.arn],
          )
        }
      },
    ]
  })
}

# ── Policy alarm ───────────────────────────────────────────────────────────
# Only meaningful now that the engine is in ENFORCE: in LOG_ONLY a mismatch
# costs nothing, whereas under ENFORCE it is a refused tool call. Threshold is
# deliberately not 1 - a single mismatch is noise, a sustained run is a signal.
#
# Cost: one alarm, $0.10/month.

resource "aws_cloudwatch_metric_alarm" "policy_mismatch" {
  alarm_name        = "${local.name}-policy-mismatch"
  alarm_description = "Cedar refused tool calls on the cloudprice gateway. Either the allow-list has drifted from the deployed tool schema, or something is requesting tools it should not have."

  namespace   = "AWS/Bedrock-AgentCore"
  metric_name = "TotalMismatchedPolicies"
  dimensions = {
    TargetResource = aws_bedrockagentcore_gateway.cloudprice.gateway_id
    OperationName  = "AuthorizeAction"
    PolicyEngine   = aws_bedrockagentcore_policy_engine.cloudprice.policy_engine_id
  }

  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 2
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"

  # Without this a quiet period reads as a breach on a Sum metric.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
