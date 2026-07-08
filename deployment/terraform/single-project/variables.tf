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

variable "telemetry_logs_filter" {
  type        = string
  description = "Log Sink filter for capturing telemetry data. Captures logs with the `traceloop.association.properties.log_type` attribute set to `tracing`."
  default     = "labels.service_name=\"hermes\" labels.type=\"agent_telemetry\""
}

variable "feedback_logs_filter" {
  type        = string
  description = "Log Sink filter for capturing feedback data. Captures logs where the `log_type` field is `feedback`."
  default     = "jsonPayload.log_type=\"feedback\" jsonPayload.service_name=\"hermes\""
}

variable "llm_cost_logs_filter" {
  type        = string
  description = "Log Sink filter for per-call LLM cost telemetry. Captures the structured `llm.call` events emitted by obs.llm_cost.record_llm_call (token counts + computed USD cost per matching/tailoring/profile-extract call)."
  default     = "jsonPayload.message=\"llm.call\""
}

variable "app_sa_roles" {
  description = "List of roles to assign to the application service account"
  type        = list(string)
  default = [

    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/storage.admin",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/cloudsql.client",
    "roles/secretmanager.secretAccessor",
  ]
}
