# The analyser jobs only log to stdout/stderr (no LOG_FILE is set in
# jobs.tf, and Cloud Run Jobs have no persistent disk anyway), so Cloud Run
# forwards everything to this project's default Cloud Logging bucket.
#
# Shortening retention here doesn't meaningfully change the bill -- storage
# within the default 30-day window is already bundled with ingestion, and
# this workload's log volume is far under the free 50 GiB/month ingestion
# allowance. This just drops routine scan logs off the default 30-day
# window down to 1 day (Cloud Logging's minimum retention granularity is a
# day, not an hour) so they don't linger longer than useful for debugging.
resource "google_logging_project_bucket_config" "default" {
  project        = var.project_id
  location       = "global"
  retention_days = 1
  bucket_id      = "_Default"

  depends_on = [google_project_service.apis]
}
