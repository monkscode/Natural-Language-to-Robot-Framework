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
#
# ⚠️  SCOPE: local development only.
# A downloaded service-account JSON key is a long-lived credential sitting on
# disk — the highest-risk credential form Google Cloud offers. Do NOT ship this
# key to production. There, authenticate without a key file:
#   • Workload Identity Federation (GitHub Actions, other clouds, on-prem)
#   • An attached service account / ADC (Cloud Run, GKE, GCE)
#   • Short-lived credentials injected by your deployment's secret manager
#
# Rotating or revoking this key (do this immediately if it ever leaks):
#   gcloud iam service-accounts keys list   --iam-account="$SA_EMAIL" --managed-by=user
#   gcloud iam service-accounts keys delete KEY_ID --iam-account="$SA_EMAIL"
# Deleting the key disables it in IAM within minutes; delete the local
# credentials.json too, then re-run this script to mint a replacement.

set -euo pipefail

# Always write credentials.json into the repo root, whatever directory the
# script was invoked from. docker-compose.vertex.yml bind-mounts it from there,
# so a key dropped in the caller's cwd would leave the container without one.
cd "$(dirname "$0")"

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

# Get a key into the current directory.
#
# `keys create` is NOT idempotent: it mints a brand-new key in IAM every time
# and overwrites the local file without asking. Re-running this script would
# therefore leave a trail of still-active keys behind and eventually hit the
# 10-keys-per-service-account limit. So reuse a valid existing key, and only
# mint a new one when there is nothing usable on disk.
KEY_FILE="credentials.json"

# True when credentials.json belongs to THIS service account and its key is
# still active in IAM (a key deleted server-side leaves a dead file behind).
existing_key_is_usable() {
    [ -f "$KEY_FILE" ] || return 1
    grep -q "\"client_email\"[[:space:]]*:[[:space:]]*\"${SA_EMAIL}\"" "$KEY_FILE" 2>/dev/null || return 1
    local key_id
    key_id=$(sed -n 's/.*"private_key_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$KEY_FILE" | head -n 1)
    [ -n "$key_id" ] || return 1
    "$GCLOUD" iam service-accounts keys describe "$key_id" \
        --iam-account="$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1
}

if existing_key_is_usable; then
    echo "ℹ️  Reusing the existing $KEY_FILE — its key is still active in IAM."
    echo "   To rotate: delete $KEY_FILE (and the old key, see below), then re-run."
else
    if [ -f "$KEY_FILE" ]; then
        echo "⚠️  $KEY_FILE exists but is not a live key for $SA_EMAIL — replacing it."
    fi
    # If this fails with FAILED_PRECONDITION, your organization blocks service-account
    # keys — see docs/VERTEX_AI_SETUP_GUIDE.md §4 for the fix.
    "$GCLOUD" iam service-accounts keys create "$KEY_FILE" \
        --iam-account="$SA_EMAIL" \
        --project="$PROJECT_ID"
fi

# Old keys stay usable until they are explicitly deleted. Surface them rather
# than deleting anything automatically — another machine or CI job may still
# be using one.
#
# The listing is captured separately from the count so a gcloud failure
# (permission or API error) is reported instead of silently reading as "0 keys",
# which would suppress the warning exactly when it matters. --filter drops
# disabled keys so the number matches what the message claims.
KEY_LIST=""
if KEY_LIST=$("$GCLOUD" iam service-accounts keys list \
        --iam-account="$SA_EMAIL" --project="$PROJECT_ID" \
        --managed-by=user --filter="disabled=false" \
        --format="value(name)" 2>/dev/null); then
    KEY_COUNT=$(printf '%s' "$KEY_LIST" | grep -c . || true)
    KEY_COUNT=$(printf '%s' "${KEY_COUNT:-0}" | tr -d '[:space:]')
    if [ "${KEY_COUNT:-0}" -gt 1 ]; then
        echo ""
        echo "⚠️  $SA_EMAIL now has $KEY_COUNT enabled user-managed keys (limit: 10)."
        echo "   Delete the ones you no longer use:"
        echo "     gcloud iam service-accounts keys list --iam-account=$SA_EMAIL --managed-by=user"
        echo "     gcloud iam service-accounts keys delete KEY_ID --iam-account=$SA_EMAIL"
    fi
else
    echo ""
    echo "⚠️  Could not list existing keys for $SA_EMAIL (permission or API error)."
    echo "   Check the key count yourself before creating more — the limit is 10:"
    echo "     gcloud iam service-accounts keys list --iam-account=$SA_EMAIL --managed-by=user"
fi

echo ""
echo "✅ Done! $KEY_FILE is ready in: $(pwd)"
echo "➡️  In src/backend/.env set:"
echo "      MODEL_PROVIDER=vertex"
echo "      VERTEXAI_PROJECT=$PROJECT_ID"
echo "      VERTEXAI_LOCATION=<your region, e.g. us-central1>"
echo "      VERTEXAI_CREDENTIALS=credentials.json   # already the default; in Docker the"
echo "                                              # vertex compose override replaces it"
echo "                                              # with the in-container path"
echo "⚠️  Never commit $KEY_FILE to Git (it is already in .gitignore)."
echo "⚠️  This key is for LOCAL DEVELOPMENT. In production use Workload Identity"
echo "   Federation, an attached service account, or secret-manager injection."
echo "   If it ever leaks, revoke it:"
echo "     gcloud iam service-accounts keys delete KEY_ID --iam-account=$SA_EMAIL"
