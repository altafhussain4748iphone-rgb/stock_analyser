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

variable "github_owner" {
  description = "GitHub org/user that owns the source repo (for the Cloud Build trigger)."
  type        = string
  default     = "altafhussain4748iphone-rgb"
}

variable "github_repo" {
  description = "GitHub repo name (for the Cloud Build trigger)."
  type        = string
  default     = "stock_analyser"
}

variable "github_branch" {
  description = "Branch that triggers a build/deploy on push."
  type        = string
  default     = "^main$"
}

variable "container_image" {
  description = <<-EOT
    Image used when a Cloud Run Job is first created. A public placeholder is
    used because Cloud Run requires an existing, pullable image at creation
    time and Artifact Registry starts out empty. Once the Cloud Build trigger
    runs (on the first push to main) it repoints each job at the real image,
    and `ignore_changes` on the job's container spec stops Terraform from
    reverting that on subsequent applies.
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
