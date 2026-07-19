# Deployment: Google Cloud Run + Terraform (local-only)

Scheduling runs on Google Cloud; deploys are always run from your machine — there is no CI/CD and no GitHub↔GCP connection. GitHub is used purely as a code host.

- **Cloud Run Jobs** run each workflow (`crypto`, and `stocks` once implemented) to completion in a container — a better fit than a long-lived service for a script that runs, sends alerts, and exits.
- **Cloud Scheduler** triggers each job's Cloud Run execution every 15 minutes (configurable per job).
- **`scripts/deploy.sh`** builds the Docker image locally, pushes it to **Artifact Registry**, and updates the Cloud Run Job(s) to use it. You run this yourself whenever you want to ship a code change.
- **Secret Manager** holds SMTP credentials and email addresses; the job's runtime service account reads them at execution time.
- All logs go to **Cloud Logging** automatically (stdout/stderr from every execution).

All infrastructure is defined in [terraform/](terraform/), with the reusable per-workflow pieces (Cloud Run Job + Cloud Scheduler trigger) factored into [terraform/modules/analyser_job/](terraform/modules/analyser_job/). Terraform state is stored remotely in the `financial-analyser` GCS bucket.

## One-time setup
1. **State bucket** — `financial-analyser` already exists; nothing to do.
2. **Authenticate locally** for both Terraform and `gcloud`:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=$(pwd)/google_credentials.json
   gcloud auth activate-service-account --key-file="$GOOGLE_APPLICATION_CREDENTIALS"
   gcloud config set project financial-analyser-502901
   ```
3. **Set secret values.** Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars` (gitignored) and fill in real SMTP/email values, or export them as `TF_VAR_smtp_user`, `TF_VAR_smtp_pass`, `TF_VAR_email_from`, `TF_VAR_email_to`.
4. **Provision infrastructure:**
   ```bash
   cd terraform
   terraform init
   terraform apply
   cd ..
   ```
   This creates the Cloud Run Job(s), Cloud Scheduler trigger(s), service accounts, and secrets — the job starts out pointing at a public placeholder image, since Artifact Registry is empty on a fresh project.
5. **Deploy the real image:**
   ```bash
   ./scripts/deploy.sh
   ```
   Builds the image, pushes it to Artifact Registry, and updates every job listed in `ALL_JOBS` inside the script to use it. From then on, `terraform apply` won't touch the deployed image (`lifecycle.ignore_changes` in the module) — `scripts/deploy.sh` owns image updates.

## Everyday workflow
After changing code:
```bash
./scripts/deploy.sh            # deploy every job
./scripts/deploy.sh crypto     # or just one
```
Push to GitHub whenever you like (`git push`) — it's just for code history/backup and doesn't trigger anything on GCP.

If you change `terraform/*.tf` (new job, new schedule, new secret, etc.), run `terraform apply` from `terraform/` as well.

## Extending to a new script
To schedule another script (e.g. once `technical_analysis/stocks/alerts.py` is filled in):
1. Uncomment/copy the `module "stocks_analyser"` block in [terraform/jobs.tf](terraform/jobs.tf), adjusting `name`, `workspace_arg`, and `schedule`.
2. `terraform apply`.
3. Add `"stocks"` to the `ALL_JOBS` array in [scripts/deploy.sh](scripts/deploy.sh) so `./scripts/deploy.sh` (with no args) picks it up too.

No new image or Dockerfile is needed — every workflow shares the same image, and the CLI workspace argument (`crypto`/`stocks`) picks which one runs.

## Viewing logs
Every Cloud Run Job execution's stdout/stderr is captured in Cloud Logging automatically:
- **Console:** Cloud Run → Jobs → `analyser-crypto` → Logs (or use the `crypto_job_logs_url` Terraform output).
- **CLI:** `gcloud logging read 'resource.type="cloud_run_job" resource.labels.job_name="analyser-crypto"' --project=financial-analyser-502901 --limit=50 --order=desc`
