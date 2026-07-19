variable "project_id" {
  description = "GCP project that hosts all analyser infrastructure."
  type        = string
  default     = "financial-analyser-502901"
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry, and Cloud Scheduler."
  type        = string
  default     = "us-central1"
}

variable "container_image" {
  description = <<-EOT
    Image used when a Cloud Run Job is first created. A public placeholder is
    used because Cloud Run requires an existing, pullable image at creation
    time and Artifact Registry starts out empty. Run `scripts/deploy.sh`
    right after the first `terraform apply` to build and push the real
    image and point each job at it -- `ignore_changes` on the job's
    container spec stops subsequent `terraform apply` runs from reverting
    that back to this placeholder.
  EOT
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/job:latest"
}

variable "smtp_host" {
  description = "SMTP server host."
  type        = string
  default     = "smtp.gmail.com"
}

variable "smtp_port" {
  description = "SMTP server port."
  type        = string
  default     = "587"
}

variable "smtp_user" {
  description = "SMTP username (e.g. sending Gmail address). Stored in Secret Manager, never in state as plain resource config."
  type        = string
  sensitive   = true
}

variable "smtp_pass" {
  description = "SMTP password / app password. Stored in Secret Manager."
  type        = string
  sensitive   = true
}

variable "email_from" {
  description = "From address for alert emails."
  type        = string
  sensitive   = true
}

variable "email_to" {
  description = "Recipient address for alert emails."
  type        = string
  sensitive   = true
}
