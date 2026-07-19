# Each analyser workflow gets its own Cloud Run Job + Cloud Scheduler trigger.
# To add a new script once it's ready (e.g. technical_analysis/stocks/alerts.py
# is implemented): copy this block, change `name`/`workspace_arg`/`schedule`,
# `terraform apply`, then add its name to ALL_JOBS in scripts/deploy.sh so
# local deploys pick it up too.
module "crypto_analyser" {
  source = "./modules/analyser_job"

  project_id      = var.project_id
  region          = var.region
  name            = "crypto"
  workspace_arg   = "crypto"
  image           = var.container_image
  schedule        = "*/15 * * * *"
  timeout_seconds = 900
  parallelism     = 2

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

# Same configs as crypto_analyser above, but a separate job/schedule since it
# runs a different scan (technical_analysis.crypto.alerts.run_volume_price_scan):
# volume >= 3x median and price move >= 1.5% on the closed candle, no breakout
# filters.
module "crypto_volume_analyser" {
  source = "./modules/analyser_job"

  project_id      = var.project_id
  region          = var.region
  name            = "crypto-volume"
  workspace_arg   = "crypto-volume"
  image           = var.container_image
  schedule        = "*/15 * * * *"
  timeout_seconds = 900
  parallelism     = 2

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
