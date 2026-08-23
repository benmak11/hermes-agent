# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

# Backing store for the GenAI completions telemetry pipeline in telemetry.tf
# (the external table and its BigQuery connection both read from it).
# NOT YET APPLIED — this bucket does not exist and LOGS_BUCKET_NAME is unset on
# the live services, so nothing writes completions today. See README.md.
resource "google_storage_bucket" "logs_data_bucket" {
  name                        = "${var.project_id}-${var.project_name}-logs"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true

  depends_on = [resource.google_project_service.services]
}
