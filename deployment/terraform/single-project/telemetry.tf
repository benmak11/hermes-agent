# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

# ====================================================================
# LLM cost telemetry — the only telemetry pipeline that is applied.
# ====================================================================
#
# Everything in this file is in terraform state and live in the project. The
# GCS-backed GenAI *completions* pipeline that used to sit here — a BigQuery
# connection, an external table over a logs bucket, a completions view, and
# sinks for genai/feedback logs — was never applied in ~4 months, wrote
# nothing (LOGS_BUCKET_NAME is unset on both services, so no completions are
# emitted to store), and made `terraform plan` unreadable by proposing a dozen
# resources on every run. It was removed 2026-08-30 along with its bucket in
# storage.tf.
#
# Re-adding prompt/response capture later is a deliberate act, not a recovery:
# it means putting full prompt text — which includes résumé content — in a
# bucket, and that is a decision worth making explicitly.

# BigQuery dataset holding the cost telemetry table.
resource "google_bigquery_dataset" "telemetry_dataset" {
  project       = var.project_id
  dataset_id    = replace("${var.project_name}_telemetry", "-", "_")
  friendly_name = "${var.project_name} Telemetry"
  location      = var.region
  description   = "Dataset for Hermes LLM cost telemetry"
  depends_on    = [google_project_service.services]
}

# Log sink for per-call LLM cost telemetry (obs.llm_cost.record_llm_call).
# No table is pre-created: the event is self-contained — no join against
# GCS-stored prompt/response data is needed to compute cost — so Cloud
# Logging's auto-created, schema-evolved table is sufficient. Query
# cost-per-run with `GROUP BY run_id`, cost-per-job with `GROUP BY job_id`
# (see the root README's cost-telemetry section).
#
# This is the cross-check on the per-run ledger at users/{uid}/runs/{run_id}:
# the ledger is written by the app and banks at-least-once, this sink is
# written by Cloud Logging and does not. Do not remove one assuming the other
# covers it.
resource "google_logging_project_sink" "llm_cost_logs_to_bq" {
  name        = "${var.project_name}-llm-cost"
  project     = var.project_id
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.telemetry_dataset.dataset_id}"
  filter      = var.llm_cost_logs_filter

  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }

  depends_on = [google_bigquery_dataset.telemetry_dataset]
}

resource "google_bigquery_dataset_iam_member" "llm_cost_logs_bq_writer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.telemetry_dataset.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.llm_cost_logs_to_bq.writer_identity
}
