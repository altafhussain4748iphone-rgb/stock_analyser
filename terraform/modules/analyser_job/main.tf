# A Cloud Run Job runs a container to completion on a schedule (as opposed to
# a Cloud Run *Service*, which serves HTTP traffic) -- the right fit for a
# batch script like the crypto/stocks analysers.
resource "google_cloud_run_v2_job" "this" {
  project  = var.project_id
  name     = "analyser-${var.name}"
  location = var.region

  template {
    template {
      service_account = var.service_account_email
      timeout         = "${var.timeout_seconds}s"
      max_retries     = var.max_retries

      containers {
        image = var.image
        args  = [var.workspace_arg]

        dynamic "env" {
          for_each = var.env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = var.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    # Cloud Build (see terraform/cloudbuild.tf) redeploys new images with
    # `gcloud run jobs update --image=...` on every push to main. Ignore the
    # image here so a routine `terraform apply` doesn't revert deploys back
    # to the bootstrap placeholder.
    ignore_changes = [template[0].template[0].containers[0].image]
  }
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.this.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.scheduler_service_account_email}"
}

resource "google_cloud_scheduler_job" "this" {
  project   = var.project_id
  region    = var.region
  name      = "analyser-${var.name}-trigger"
  schedule  = var.schedule
  time_zone = var.time_zone

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.this.name}:run"

    oauth_token {
      service_account_email = var.scheduler_service_account_email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}
