variable "region" {
  description = "AWS region. Must match the region the AgentCore Gateway is created in."
  type        = string
  default     = "us-east-1"
}

variable "memory_mb" {
  description = "Lambda memory. Also scales CPU, so this mostly buys cold-start speed while the price catalog is parsed."
  type        = number
  default     = 512
}

variable "timeout_seconds" {
  description = "Lambda timeout. Tools are pure computation over local JSON; anything slow is a bug, not load."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch retention for the function's logs."
  type        = number
  default     = 14
}

variable "alert_email" {
  description = "Address subscribed to the agent alert topic. Confirm the subscription email after the first apply."
  type        = string
  default     = "baker.ali.m@gmail.com"
}

variable "profile_token_threshold" {
  description = "Hourly input-token ceiling per inference profile before alarming. Set well above expected traffic so normal use never trips it."
  type        = number
  default     = 500000
}

variable "policy_engine_mode" {
  description = "LOG_ONLY records Cedar decisions without blocking; ENFORCE denies."
  type        = string
  # Promoted from LOG_ONLY on 2026-09-06.
  #
  # The original plan was to soak in LOG_ONLY for a week and read the decisions
  # before promoting. That plan could never have completed: there is no
  # policy-decision log group in this account, so the decisions were being
  # recorded nowhere. LOG_ONLY with no sink is strictly worse than ENFORCE - it
  # blocks nothing AND tells you nothing.
  #
  # Promoting is safe here for one specific reason: the allow-list is GENERATED
  # from gateway_lambda/tool-schema.json, the same file the gateway target is
  # built from, so it cannot drift from the tools that exist. Verified before
  # the flip - the live gateway advertises 25 tools and the schema has 25.
  #
  # What changes in behaviour: forbid_absurd_sizing starts actually rejecting
  # implausible vcpu counts instead of merely noting them, and a tool added
  # without regenerating the schema is denied rather than silently allowed.
  # That second property is the whole point.
  default = "ENFORCE"

  validation {
    condition     = contains(["LOG_ONLY", "ENFORCE"], var.policy_engine_mode)
    error_message = "policy_engine_mode must be LOG_ONLY or ENFORCE."
  }
}

variable "harness_max_iterations" {
  description = "Cap on the agent loop. Stops a non-terminating tool-call cycle from becoming a bill."
  type        = number
  default     = 10
}

variable "harness_max_tokens" {
  description = "Execution token ceiling per invocation, across the whole loop."
  type        = number
  default     = 40000
}

variable "memory_expiry_days" {
  description = "How long memory events are retained. Both a cost line and a data-retention decision."
  type        = number
  default     = 30
}

variable "trail_retention_days" {
  description = "How long CloudTrail logs are kept in S3."
  type        = number
  default     = 365
}
