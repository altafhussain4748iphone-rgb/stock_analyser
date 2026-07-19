variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  description = "Short identifier for this analyser workflow, e.g. \"crypto\" or \"stocks\"."
  type        = string
}

variable "workspace_arg" {
  description = "Argument passed to `python -m technical_analysis.cli <workspace_arg>`."
  type        = string
}

variable "image" {
  type = string
}

variable "service_account_email" {
  description = "Runtime SA the job executes as."
  type        = string
}

variable "scheduler_service_account_email" {
  description = "SA Cloud Scheduler uses to invoke the job."
  type        = string
}

variable "schedule" {
  description = "Cron schedule (unix-cron syntax) for how often to run this job."
  type        = string
  default     = "*/15 * * * *"
}

variable "time_zone" {
  type    = string
  default = "Etc/UTC"
}

variable "env" {
  description = "Plain (non-secret) environment variables."
  type        = map(string)
  default     = {}
}

variable "secret_env" {
  description = "Map of env var name -> Secret Manager secret_id, mounted as the latest secret version."
  type        = map(string)
  default     = {}
}

variable "timeout_seconds" {
  type    = number
  default = 300
}

variable "max_retries" {
  type    = number
  default = 1
}
