locals {
  required_apis = [
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "storage.googleapis.com",
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

  # Every scripts/deploy.sh run pushes a new SHA-tagged image and retags
  # :latest, leaving the previous :latest digest dangling (untagged). With no
  # cleanup, both tagged and untagged images accumulate here forever. Keep
  # the most recent 5 versions for rollback, and let anything older than 30
  # days get swept regardless of tag state.
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent-versions"
    action = "KEEP"

    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-old-versions"
    action = "DELETE"

    condition {
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.apis]
}
