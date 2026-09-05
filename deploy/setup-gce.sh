#!/usr/bin/env bash
# ==============================================================================
# Deploy Second Brain Agent to Google Cloud Compute Engine (e2-micro — Free Tier)
# ==============================================================================
#
# Prerequisites:
#   1. gcloud CLI installed and authenticated
#   2. A GCP project with billing enabled
#   3. Docker image pushed to Artifact Registry or Container Registry
#
# Usage:
#   chmod +x deploy/setup-gce.sh
#   ./deploy/setup-gce.sh
# ==============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get project)}"
ZONE="us-central1-a"  # Free tier zone
INSTANCE_NAME="second-brain-agent"
MACHINE_TYPE="e2-micro"
DISK_SIZE="30"  # GB (free tier allows 30GB)
IMAGE_NAME="second-brain-agent"
REGION="us-central1"
REPO_NAME="second-brain"

echo "============================================================"
echo "🧠 Deploying Second Brain Agent to GCE"
echo "============================================================"
echo "Project:  ${PROJECT_ID}"
echo "Zone:     ${ZONE}"
echo "Machine:  ${MACHINE_TYPE}"
echo "============================================================"

# ── Step 1: Create Artifact Registry Repository ──────────────────────────────
echo ""
echo "📦 Step 1: Setting up Artifact Registry..."
gcloud artifacts repositories describe "${REPO_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" 2>/dev/null || \
gcloud artifacts repositories create "${REPO_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --repository-format=docker \
    --description="Second Brain Agent container images"

# ── Step 2: Build and Push Docker Image ──────────────────────────────────────
echo ""
echo "🐳 Step 2: Building and pushing Docker image..."
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${IMAGE_NAME}:latest"

# Configure Docker for Artifact Registry
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# Build and push
docker build -t "${IMAGE_URI}" .
docker push "${IMAGE_URI}"

echo "Image pushed: ${IMAGE_URI}"

# ── Step 3: Create or Update GCE Instance ────────────────────────────────────
echo ""
echo "🖥️ Step 3: Creating GCE instance..."

# Check if instance exists
if gcloud compute instances describe "${INSTANCE_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" 2>/dev/null; then
    echo "Instance already exists. Updating container..."
    gcloud compute instances update-container "${INSTANCE_NAME}" \
        --project="${PROJECT_ID}" \
        --zone="${ZONE}" \
        --container-image="${IMAGE_URI}"
else
    # Create new instance with Container-Optimized OS
    gcloud compute instances create-with-container "${INSTANCE_NAME}" \
        --project="${PROJECT_ID}" \
        --zone="${ZONE}" \
        --machine-type="${MACHINE_TYPE}" \
        --image-family=cos-stable \
        --image-project=cos-cloud \
        --boot-disk-size="${DISK_SIZE}GB" \
        --container-image="${IMAGE_URI}" \
        --container-mount-host-path=host-path=/mnt/stateful_partition/second-brain-data,mount-path=/app/data \
        --container-env-file=.env \
        --tags=second-brain \
        --scopes=cloud-platform \
        --metadata=google-logging-enabled=true
fi

echo ""
echo "============================================================"
echo "✅ Deployment complete!"
echo ""
echo "Useful commands:"
echo "  # View logs"
echo "  gcloud compute ssh ${INSTANCE_NAME} --zone=${ZONE} --command='docker logs -f \$(docker ps -q)'"
echo ""
echo "  # SSH into instance"
echo "  gcloud compute ssh ${INSTANCE_NAME} --zone=${ZONE}"
echo ""
echo "  # Restart container"
echo "  gcloud compute instances reset ${INSTANCE_NAME} --zone=${ZONE}"
echo ""
echo "  # Delete instance"
echo "  gcloud compute instances delete ${INSTANCE_NAME} --zone=${ZONE}"
echo "============================================================"
