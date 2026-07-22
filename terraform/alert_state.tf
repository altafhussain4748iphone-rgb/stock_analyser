# Cloud Run Jobs have no persistent local disk across executions, so alert
# cooldown/dedup state (written by technical_analysis/crypto/alerts.py's
# load_state/save_state) needs somewhere durable to live between runs. This
# bucket holds a single small JSON object for that -- kept separate from the
# `financial-analyser` Terraform-state bucket so the job's runtime SA never
# needs any access to Terraform's own state.
resource "google_storage_bucket" "alert_state" {
  project                     = var.project_id
  name                        = "${var.project_id}-alert-state"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  # The state object is overwritten on every scan; cap version history so
  # this doesn't grow unbounded.
  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# Scoped to this one bucket -- not a project-level storage role -- so the
# job's runtime SA can only read/write its own state object, nothing else.
resource "google_storage_bucket_iam_member" "runtime_alert_state_writer" {
  bucket = google_storage_bucket.alert_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.analyser_runtime.email}"
}
