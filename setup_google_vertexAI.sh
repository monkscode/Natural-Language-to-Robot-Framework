#!/usr/bin/env bash
# setup_google_vertexAI.sh — one-time Google Vertex AI setup for Mark 1.
#
# Logs you into Google Cloud, enables the Vertex AI API, creates a dedicated
# service account, and downloads its key as credentials.json in the current
# directory. Run it from the repo root (on Windows, use Git Bash):
#
#     bash setup_google_vertexAI.sh
#
# Prerequisite: the gcloud CLI — https://cloud.google.com/sdk/docs/install
# If it is not installed yet, the script does NOT fail: it waits while you
# install it, then continues automatically in the same window.
# Full walkthrough (incl. organization-policy fixes): docs/VERTEX_AI_SETUP_GUIDE.md

set -euo pipefail

# Locate the gcloud CLI: on PATH, or in the default install folders (a fresh
# install is not visible on this shell's PATH yet, so check those too).
find_gcloud() {
    if command -v gcloud >/dev/null 2>&1; then
        echo "gcloud"
        return 0
    fi
    local candidate
    for candidate in \
        "$HOME/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd" \
        "/c/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd" \
        "/c/Program Files/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd" \
        "$HOME/google-cloud-sdk/bin/gcloud" \
        "/usr/local/google-cloud-sdk/bin/gcloud"; do
        if [ -f "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

GCLOUD="$(find_gcloud || true)"
while [ -z "$GCLOUD" ]; do
    echo ""
    echo "⚠️  The gcloud CLI is not installed yet — no problem, this window will wait for you."
    echo "   1. Install it from: https://cloud.google.com/sdk/docs/install"
    echo "      (pick your OS and follow the steps; keep this window open meanwhile)"
    echo "   2. When the installation finishes, come back here."
    echo ""
    read -r -p "👉 Press Enter once the gcloud CLI is installed, and I'll continue automatically... " _ \
        || { echo "Aborted."; exit 1; }
    GCLOUD="$(find_gcloud || true)"
    if [ -z "$GCLOUD" ]; then
        echo "🔎 Still can't find gcloud — if you did install it, it may be in a custom location."
        echo "   Open a NEW terminal window and re-run: bash setup_google_vertexAI.sh"
    fi
done
echo "✅ gcloud CLI found: $GCLOUD"

# Log in and select your Google Cloud project
"$GCLOUD" init

# Read the project ID you just selected — no need to type it yourself
# (tr also strips the CRLF line endings gcloud.cmd emits on Windows)
PROJECT_ID=$("$GCLOUD" config get-value project 2>/dev/null | tr -d '[:space:]' || true)
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    echo "❌ No project selected. Re-run this script and choose a project during 'gcloud init'."
    exit 1
fi
echo "✅ Using Google Cloud project: $PROJECT_ID"

# Enable the Vertex AI API for the project
"$GCLOUD" services enable aiplatform.googleapis.com --project="$PROJECT_ID"

# Create a dedicated service account (reused if it already exists)
SA_EMAIL="vertex-ai-sa@${PROJECT_ID}.iam.gserviceaccount.com"
if "$GCLOUD" iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "ℹ️  Service account $SA_EMAIL already exists — reusing it."
else
    "$GCLOUD" iam service-accounts create vertex-ai-sa \
        --display-name="Vertex AI Service Account" \
        --project="$PROJECT_ID"
fi

# Grant it the Vertex AI User role
"$GCLOUD" projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user"

# Download the key into the current directory
# If this fails with FAILED_PRECONDITION, your organization blocks service-account
# keys — see docs/VERTEX_AI_SETUP_GUIDE.md §4 for the fix.
"$GCLOUD" iam service-accounts keys create credentials.json \
    --iam-account="$SA_EMAIL" \
    --project="$PROJECT_ID"

echo ""
echo "✅ Done! credentials.json created in: $(pwd)"
echo "➡️  Set VERTEXAI_PROJECT=$PROJECT_ID in src/backend/.env"
echo "⚠️  Never commit credentials.json to Git (it is already in .gitignore)."
