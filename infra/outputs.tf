output "lambda_function_arn" {
  description = "ARN of the Gateway target. Needed when creating the gateway target in step 4."
  value       = aws_lambda_function.gateway_target.arn
}

output "lambda_function_name" {
  description = "Function name, for direct test invokes."
  value       = aws_lambda_function.gateway_target.function_name
}

output "execution_role_arn" {
  description = "Execution role. CloudWatch Logs only, by design."
  value       = aws_iam_role.lambda.arn
}

output "gateway_id" {
  description = "AgentCore Gateway id."
  value       = aws_bedrockagentcore_gateway.cloudprice.gateway_id
}

output "gateway_url" {
  description = "MCP endpoint. This is what an AI client connects to."
  value       = aws_bedrockagentcore_gateway.cloudprice.gateway_url
}

output "inference_profile_orchestrator_arn" {
  description = "Tagged profile for the reasoning role - use this as the model id, not a bare model."
  value       = aws_bedrock_inference_profile.orchestrator.arn
}

output "inference_profile_recommender_arn" {
  description = "Tagged profile for the high-volume role."
  value       = aws_bedrock_inference_profile.recommender.arn
}

output "dashboard_url" {
  description = "CloudWatch dashboard showing usage split by agent role."
  value       = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.agent.dashboard_name}"
}

output "bedrock_invocation_log_group" {
  description = "Where model invocation logs land. Contains prompt and completion text."
  value       = aws_cloudwatch_log_group.bedrock_invocations.name
}

output "harness_id" {
  description = "AgentCore harness id - what the website will invoke."
  value       = aws_bedrockagentcore_harness.cloudprice.harness_id
}

output "harness_arn" {
  description = "Harness ARN."
  value       = aws_bedrockagentcore_harness.cloudprice.arn
}

output "inference_profile_recommender_id" {
  description = "Profile ID, not ARN. This is the value the AWS/Bedrock ModelId dimension carries, so alarms and dashboards must key on it."
  value       = aws_bedrock_inference_profile.recommender.id
}

output "inference_profile_orchestrator_id" {
  description = "Profile ID for the orchestrator role, for CloudWatch dimensions."
  value       = aws_bedrock_inference_profile.orchestrator.id
}
