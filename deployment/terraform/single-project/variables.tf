# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

variable "project_name" {
  type        = string
  description = "Project name used as a base for resource naming"
  default     = "hermes"
}

variable "project_id" {
  type        = string
  description = "Google Cloud Project ID for resource deployment."
}

variable "region" {
  type        = string
  description = "Google Cloud region for resource deployment."
  default     = "us-central1"
}

variable "llm_cost_logs_filter" {
  type        = string
  description = "Log Sink filter for per-call LLM cost telemetry. Captures the structured `llm.call` events emitted by obs.llm_cost.record_llm_call (token counts + computed USD cost per matching/tailoring/profile-extract call)."
  default     = "jsonPayload.message=\"llm.call\""
}

# NOTE: the runtime service account (hermes-runtime@) and the roles bound to it
# are managed by hand, not here — see README.md. The old `app_sa_roles` variable
# and the `hermes-app` service account it configured were removed: that account
# was never created and nothing referenced it.
