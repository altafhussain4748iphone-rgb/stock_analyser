# Deployment: Google Cloud Run + Terraform

Scheduling and deploys run on Google Cloud rather than GitHub Actions:

- **Cloud Run Jobs** run each workflow (`crypto`, and `stocks` once implemented) to completion in a container — a better fit than a long-lived service for a script that runs, sends alerts, and exits.
- **Cloud Scheduler** triggers each job's Cloud Run execution every 15 minutes (configurable per job).
- **Cloud Build**, triggered on every push to `main`, builds the Docker image, pushes it to **Artifact Registry**, and redeploys the Cloud Run Job — this is what replaces the old GitHub Actions workflow for deploys.
- **Secret Manager** holds SMTP credentials and email addresses; the job's runtime service account reads them at execution time.
- All logs go to **Cloud Logging** automatically (stdout/stderr from every execution).

All infrastructure is defined in [terraform/](terraform/), with the reusable per-workflow pieces (Cloud Run Job + Cloud Scheduler trigger) factored into [terraform/modules/analyser_job/](terraform/modules/analyser_job/). Terraform state is stored remotely in the `financial-analyser` GCS bucket.

## One-time setup
1. **Create the state bucket** (not managed by Terraform, to avoid a chicken-and-egg problem):
   ```bash
   gcloud storage buckets create gs://financial-analyser --project=financial-analyser-502901 --location=us-central1 --uniform-bucket-level-access
   gcloud storage buckets update gs://financial-analyser --versioning
   ```
2. **Connect the GitHub repo to Cloud Build** (one manual console step Terraform can't perform — it requires GitHub OAuth consent): Console → Cloud Build → Triggers → Connect Repository → install the Google Cloud Build GitHub App on `stock_analyser`.
3. **Set secret values.** Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars` (gitignored) and fill in real SMTP/email values, or export them as `TF_VAR_smtp_user`, `TF_VAR_smtp_pass`, `TF_VAR_email_from`, `TF_VAR_email_to`.
4. **Deploy:**
   ```bash
   cd terraform
   export GOOGLE_APPLICATION_CREDENTIALS=$(pwd)/../google_credentials.json  # or use `gcloud auth application-default login`
   terraform init
   terraform apply
   ```
   The first apply creates each Cloud Run Job pointing at a public placeholder image (Artifact Registry starts out empty). Push to `main` afterward to trigger Cloud Build, which builds the real image and redeploys the job — from then on, Terraform leaves the deployed image alone (see the `lifecycle.ignore_changes` in the module) and Cloud Build owns image updates.

## Extending to a new script
To schedule another script (e.g. once `technical_analysis/stocks/alerts.py` is filled in):
1. Uncomment/copy the `module "stocks_analyser"` block in [terraform/jobs.tf](terraform/jobs.tf), adjusting `name`, `workspace_arg`, and `schedule`.
2. Add a matching `gcloud run jobs update analyser-stocks ...` step to [cloudbuild.yaml](cloudbuild.yaml) so pushes deploy it too.
3. `terraform apply`.

No new image or Dockerfile is needed — every workflow shares the same image, and the CLI workspace argument (`crypto`/`stocks`) picks which one runs.

## Viewing logs
Every Cloud Run Job execution's stdout/stderr is captured in Cloud Logging automatically:
- **Console:** Cloud Run → Jobs → `analyser-crypto` → Logs (or use the `crypto_job_logs_url` Terraform output).
- **CLI:** `gcloud logging read 'resource.type="cloud_run_job" resource.labels.job_name="analyser-crypto"' --project=financial-analyser-502901 --limit=50 --order=desc`
