# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

output "telemetry_dataset_id" {
  description = "BigQuery dataset ID for telemetry data"
  value       = google_bigquery_dataset.telemetry_dataset.dataset_id
}
