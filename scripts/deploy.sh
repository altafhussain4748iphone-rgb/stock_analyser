#!/usr/bin/env bash
# Builds the analyser image locally, pushes it to Artifact Registry, and
# repoints one or more Cloud Run Jobs at it. This is the deploy path for
# this repo -- there is no CI/CD; every deploy is run from your machine.
#
# Prerequisites: docker, gcloud (authenticated as an identity with the
# roles granted in terraform/iam.tf -- by default that's the
# terraform-deployer service account key), and the Cloud Run Jobs already
# created via `terraform apply`.
#
# Usage:
#   scripts/deploy.sh              # deploy every job in ALL_JOBS
#   scripts/deploy.sh crypto       # deploy just analyser-crypto
#   scripts/deploy.sh crypto stocks
#   scripts/deploy.sh --infra      # also run `terraform apply` after deploying
#
# --infra runs the image deploy first, then `terraform apply` from terraform/
# (still interactive -- you still see the plan and type yes). That's the
# right order when only code + existing infra settings changed. If you added
# a brand-new Cloud Run job in terraform/jobs.tf, run `terraform apply`
# manually first instead (the job must exist before this script can update
# it), then this script.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-financial-analyser-502901}"
REGION="${REGION:-us-central1}"
REPOSITORY="analyser"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/analyser"

# Keep this in sync with the `name` of each `module "..._analyser"` block in
# terraform/jobs.tf.
ALL_JOBS=(crypto crypto-volume)

WITH_INFRA=0
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--infra" ]]; then
    WITH_INFRA=1
  else
    ARGS+=("$arg")
  fi
done

if [[ ${#ARGS[@]} -gt 0 ]]; then
  JOBS=("${ARGS[@]}")
else
  JOBS=("${ALL_JOBS[@]}")
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

echo "==> Building ${IMAGE}:${TAG}"
docker build -t "${IMAGE}:${TAG}" -t "${IMAGE}:latest" "$REPO_ROOT"

echo "==> Configuring docker auth for ${REGION}-docker.pkg.dev"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet --project="${PROJECT_ID}"

echo "==> Pushing ${IMAGE}:${TAG} and :latest"
docker push "${IMAGE}:${TAG}"
docker push "${IMAGE}:latest"

for job in "${JOBS[@]}"; do
  echo "==> Deploying analyser-${job} with ${IMAGE}:${TAG}"
  gcloud run jobs update "analyser-${job}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE}:${TAG}"
done

if [[ "$WITH_INFRA" -eq 1 ]]; then
  echo "==> Running terraform apply"
  terraform -chdir="${REPO_ROOT}/terraform" apply
fi

echo "==> Done. Run one now to test:"
for job in "${JOBS[@]}"; do
  echo "    gcloud run jobs execute analyser-${job} --project=${PROJECT_ID} --region=${REGION}"
done
