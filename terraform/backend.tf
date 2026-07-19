# Remote state lives in the pre-existing "financial-analyser" GCS bucket.
# The bucket itself is not managed here (bootstrap it once, outside Terraform,
# with versioning enabled) to avoid a chicken-and-egg problem on first init.
terraform {
  backend "gcs" {
    bucket = "financial-analyser"
    prefix = "terraform/state"
  }
}
