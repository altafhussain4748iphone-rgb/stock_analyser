# Each analyser workflow gets its own Cloud Run Job + Cloud Scheduler trigger.
# To add a new script once it's ready (e.g. technical_analysis/stocks/alerts.py
# is implemented): copy this block, change `name`/`workspace_arg`/`schedule`,
# and add a matching `gcloud run jobs update analyser-<name> ...` step to
# cloudbuild.yaml so pushes deploy it too.
module "crypto_analyser" {
  source = "./modules/analyser_job"

  project_id    = var.project_id
  region        = var.region
  name          = "crypto"
  workspace_arg = "crypto"
  image         = var.container_image
  schedule      = "*/15 * * * *"

  service_account_email           = google_service_account.analyser_runtime.email
  scheduler_service_account_email = google_service_account.analyser_scheduler.email

  env = {
    SMTP_HOST           = var.smtp_host
    SMTP_PORT           = var.smtp_port
    CANDLE_INTERVAL_MIN = "15"
  }

  secret_env = {
    SMTP_USER      = google_secret_manager_secret.this["smtp-user"].secret_id
    SMTP_PASS      = google_secret_manager_secret.this["smtp-pass"].secret_id
    EMAIL_FROM     = google_secret_manager_secret.this["email-from"].secret_id
    ALERT_EMAIL_TO = google_secret_manager_secret.this["email-to"].secret_id
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.runtime_accessor,
  ]
}

# module "stocks_analyser" {
#   source = "./modules/analyser_job"
#
#   project_id    = var.project_id
#   region        = var.region
#   name          = "stocks"
#   workspace_arg = "stocks"
#   image         = var.container_image
#   schedule      = "*/15 9-16 * * MON-FRI" # e.g. US market hours
#
#   service_account_email           = google_service_account.analyser_runtime.email
#   scheduler_service_account_email = google_service_account.analyser_scheduler.email
#
#   env = {
#     SMTP_HOST = var.smtp_host
#     SMTP_PORT = var.smtp_port
#   }
#
#   secret_env = {
#     SMTP_USER      = google_secret_manager_secret.this["smtp-user"].secret_id
#     SMTP_PASS      = google_secret_manager_secret.this["smtp-pass"].secret_id
#     EMAIL_FROM     = google_secret_manager_secret.this["email-from"].secret_id
#     ALERT_EMAIL_TO = google_secret_manager_secret.this["email-to"].secret_id
#   }
#
#   depends_on = [
#     google_project_service.apis,
#     google_secret_manager_secret_iam_member.runtime_accessor,
#   ]
# }
