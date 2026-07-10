# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

locals {
  services = [
    "aiplatform.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "bigquery.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    # Phase B (queues + worker + scheduler + image registry). Appended at the
    # end on purpose: google_project_service.services is count-indexed, so
    # inserting earlier would re-key (destroy/recreate) existing entries.
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com"
  ]
}

resource "google_project_service" "services" {
  count              = length(local.services)
  project            = var.project_id
  service            = local.services[count.index]
  disable_on_destroy = false
}

resource "google_project_service_identity" "vertex_sa" {
  provider = google-beta
  project = var.project_id
  service = "aiplatform.googleapis.com"
}
