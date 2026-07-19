output "job_name" {
  value = google_cloud_run_v2_job.this.name
}

output "scheduler_job_name" {
  value = google_cloud_scheduler_job.this.name
}

output "logs_url" {
  description = "Console link to this job's execution logs."
  value       = "https://console.cloud.google.com/run/jobs/details/${var.region}/${google_cloud_run_v2_job.this.name}/logs?project=${var.project_id}"
}
