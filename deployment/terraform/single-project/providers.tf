# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

terraform {
  required_version = ">= 1.0.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.13.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
  }
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
}

provider "google" {
  alias                 = "billing_override"
  billing_project       = var.project_id
  region                = var.region
  user_project_override = true
}

# Project number, used to build the worker's Cloud Run URL (worker.tf).
data "google_project" "project" {
  project_id = var.project_id
}
