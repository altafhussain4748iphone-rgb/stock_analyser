locals {
  required_apis = [
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]

  # Deploys (scripts/deploy.sh) run locally under this same SA/key
  # (google_credentials.json), so it's granted the roles needed to push
  # images and update Cloud Run Jobs -- see iam.tf.
  deployer_email = "terraform-deployer@${var.project_id}.iam.gserviceaccount.com"
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
