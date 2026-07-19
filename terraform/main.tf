data "google_project" "current" {
  project_id = var.project_id

  # On a fresh project the Cloud Resource Manager API (which this data
  # source calls) may not be enabled yet. Forcing this to wait until after
  # google_project_service.apis is applied avoids a chicken-and-egg failure
  # on the very first `terraform apply`.
  depends_on = [google_project_service.apis]
}

locals {
  required_apis = [
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "analyser" {
  project       = var.project_id
  location      = var.region
  repository_id = "analyser"
  description   = "Container images for the financial analyser scripts (crypto, stocks, ...)."
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}
