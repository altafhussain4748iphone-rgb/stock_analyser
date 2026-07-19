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

  depends_on = [google_project_service.apis]
}

# The terraform-deployer SA doubles as the local deploy identity
# (scripts/deploy.sh): it needs to push images and update Cloud Run Job
# revisions from your machine.
resource "google_project_iam_member" "deployer_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${local.deployer_email}"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "deployer_artifactregistry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${local.deployer_email}"

  depends_on = [google_project_service.apis]
}

# `gcloud run jobs update` needs to act as the job's runtime SA.
resource "google_service_account_iam_member" "deployer_act_as_runtime" {
  service_account_id = google_service_account.analyser_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.deployer_email}"

  depends_on = [google_project_service.apis]
}
