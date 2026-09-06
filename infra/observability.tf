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
    ]
  })
}
