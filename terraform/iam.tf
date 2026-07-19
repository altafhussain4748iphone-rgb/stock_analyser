# Identity the Cloud Run Jobs run as. Kept separate from the default compute
# SA so it only has the narrow permissions the analyser scripts need.
resource "google_service_account" "analyser_runtime" {
  project      = var.project_id
  account_id   = "analyser-runtime"
  display_name = "Financial analyser job runtime"

  depends_on = [google_project_service.apis]
}

# Identity Cloud Scheduler uses to invoke Cloud Run Job executions.
resource "google_service_account" "analyser_scheduler" {
  project      = var.project_id
  account_id   = "analyser-scheduler"
  display_name = "Financial analyser Cloud Scheduler invoker"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "runtime_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.analyser_runtime.email}"
}

# Cloud Build's default SA needs to push images and deploy new job revisions.
resource "google_project_iam_member" "cloudbuild_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

resource "google_project_iam_member" "cloudbuild_artifactregistry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

# `gcloud run jobs update` needs to act as the job's runtime SA.
resource "google_service_account_iam_member" "cloudbuild_act_as_runtime" {
  service_account_id = google_service_account.analyser_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}
