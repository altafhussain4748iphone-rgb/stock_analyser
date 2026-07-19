# One-time manual step required before this trigger can fire: install/authorize
# the "Google Cloud Build" GitHub App on altafhussain4748iphone-rgb/stock_analyser
# via Console > Cloud Build > Triggers > Connect Repository. Terraform can't do
# that OAuth consent step for you, but everything else here is code-managed.
resource "google_cloudbuild_trigger" "deploy_on_push" {
  project  = var.project_id
  name     = "analyser-deploy"
  location = "global"

  github {
    owner = var.github_owner
    name  = var.github_repo
    push {
      branch = var.github_branch
    }
  }

  filename = "cloudbuild.yaml"

  substitutions = {
    _REGION     = var.region
    _REPOSITORY = google_artifact_registry_repository.analyser.repository_id
  }

  depends_on = [
    google_project_service.apis,
    google_project_iam_member.cloudbuild_run_developer,
    google_project_iam_member.cloudbuild_artifactregistry_writer,
    google_service_account_iam_member.cloudbuild_act_as_runtime,
  ]
}
