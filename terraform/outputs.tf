output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.analyser.repository_id}"
}

output "crypto_job_logs_url" {
  value = module.crypto_analyser.logs_url
}
