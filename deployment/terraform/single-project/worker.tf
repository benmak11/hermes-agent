# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
#
# Phase B ops architecture: Cloud Tasks queues feeding a dedicated
# `hermes-worker` Cloud Run service (same container image as hermes-api,
# deployed by CI outside terraform), plus the Cloud Scheduler tick that
# replaces the GitHub Actions cron, plus the Artifact Registry repo that CI
# pushes images to (build-speed step 3).
#
# The worker service itself is intentionally NOT a terraform resource — it is
# created once by hand and updated by CI, same as hermes-api/hermes-web. Its
# URL is deterministic, so the scheduler can target it before it exists (the
# scheduler just retries 404s until the first worker deploy).

locals {
  worker_service_name = "${var.project_name}-worker"
  worker_url          = "https://${local.worker_service_name}-${data.google_project.project.number}.${var.region}.run.app"

  # One queue per work type. Rate limits exist to stop retry-amplified token
  # burn and 429 storms; they are per-queue knobs, not per-user quotas (that
  # is the A5 seam). LLM work gets at most one retry — a failed cycle waits
  # for the next scheduler tick instead of hot-retrying paid calls.
  task_queues = {
    extract = { # resume/profile extraction
      max_concurrent = 5
      max_attempts   = 2
    }
    discovery = { # discovery + sweep cycles (fetch, filter, persist, score)
      max_concurrent = 3
      max_attempts   = 2
    }
    score = { # standalone scoring runs
      max_concurrent = 3
      max_attempts   = 2
    }
    tailor = { # resume tailoring
      max_concurrent = 3
      max_attempts   = 2
    }
    apply = { # Playwright application agent: never auto-retry a submission,
      # and keep concurrency low — each task drives a full browser.
      max_concurrent = 2
      max_attempts   = 1
    }
  }
}

variable "deploy_sa_email" {
  type        = string
  description = "GitHub Actions WIF deploy service account (pushes images to Artifact Registry)"
  default     = "github-deployer@hermes-agent-btm-001.iam.gserviceaccount.com"
}

resource "google_cloud_tasks_queue" "work" {
  for_each = local.task_queues

  name     = "${var.project_name}-${each.key}"
  location = var.region
  project  = var.project_id

  rate_limits {
    max_concurrent_dispatches = each.value.max_concurrent
    max_dispatches_per_second = 1
  }

  retry_config {
    max_attempts = each.value.max_attempts
    min_backoff  = "60s"
    max_backoff  = "600s"
  }

  depends_on = [google_project_service.services]
}

# Identity that Cloud Tasks and Cloud Scheduler use to call the (private)
# worker service via OIDC.
resource "google_service_account" "tasks_invoker" {
  account_id   = "${var.project_name}-tasks"
  display_name = "${var.project_name} Cloud Tasks/Scheduler invoker"
  project      = var.project_id
  depends_on   = [google_project_service.services]
}

# Project-level run.invoker rather than per-service IAM: the worker service is
# created by gcloud (outside terraform), so a per-service binding here would
# fail to plan until the service exists. This SA is used for nothing else.
resource "google_project_iam_member" "tasks_invoker_run" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.tasks_invoker.email}"
}

# Hourly tick, replacing .github/workflows/discovery-tick.yml (and the
# CRON_SECRET it needed — OIDC is the auth now). The endpoint fans out
# per-user named tasks and returns; per-user cadence settings gate the
# actual work, so hourly is safe and cheap.
resource "google_cloud_scheduler_job" "discovery_tick" {
  name             = "${var.project_name}-discovery-tick"
  description      = "Tick the auto-discovery scheduler for every user"
  project          = var.project_id
  region           = var.region
  schedule         = "0 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "180s"

  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
  }

  http_target {
    http_method = "POST"
    uri         = "${local.worker_url}/internal/cron/tick"
    oidc_token {
      service_account_email = google_service_account.tasks_invoker.email
      audience              = local.worker_url
    }
  }

  depends_on = [google_project_service.services]
}

# Image registry for CI (build-speed step 3): buildx pushes here, Cloud Run
# deploys by digest/tag instead of rebuilding from source every time.
resource "google_artifact_registry_repository" "images" {
  repository_id = var.project_name
  format        = "DOCKER"
  location      = var.region
  project       = var.project_id
  description   = "hermes service images (pushed by CI)"

  # The API image is ~2GB (Playwright base); without cleanup every deploy
  # accumulates storage cost forever.
  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-stale"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  project    = var.project_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.deploy_sa_email}"
}

output "worker_url" {
  value       = local.worker_url
  description = "Deterministic URL of the hermes-worker Cloud Run service"
}

output "tasks_invoker_email" {
  value       = google_service_account.tasks_invoker.email
  description = "SA email for TASKS_SA_EMAIL env var on hermes-api/worker"
}
