# Google Cloud Vertex AI: Authentication & Setup Guide

This guide explains how to authenticate and use Google Cloud Vertex AI (Gemini) locally and within Docker containers. It also covers how to bypass the "Secure by Default" Organization Policy that blocks Service Account JSON key creation.

---

## 1. Initial Setup (Local Machine)

Before interacting with Vertex AI, you need the Google Cloud CLI installed:

1. **Install gcloud CLI:** [Download link](https://cloud.google.com/sdk/docs/install)
2. **Initialize gcloud:** Open a terminal and run:
   ```bash
   gcloud init
   ```
   *Log in with your Google account and select your active project (e.g., `project-1434b01c-dc17-4123-ba3`).*
3. **Enable Vertex AI API:** Ensure the API is active for your project:
   ```bash
   gcloud services enable aiplatform.googleapis.com --project=project-1434b01c-dc17-4123-ba3
   ```

---

## 2. Local Development Login (ADC)

For quick local development (running scripts directly on your machine without Docker), use **Application Default Credentials (ADC)**. 

Run this command to authenticate:
```bash
gcloud auth application-default login
```
* **How it works:** This downloads a local JSON file (usually to `%APPDATA%\gcloud\application_default_credentials.json` on Windows) containing a refresh token.
* **Limitations for Docker/Production:** The access tokens expire every 60 minutes (the SDK auto-refreshes them), but the underlying refresh token itself can expire depending on your Workspace session controls (e.g., every 14 days), requiring manual re-login.

---

## 3. Creating a Permanent Service Account (For Docker)

To avoid manual logins inside Docker containers, you need a dedicated **Service Account** with a long-lived JSON key that never expires.

**Step 3.1: Create the Service Account**
```bash
gcloud iam service-accounts create vertex-ai-sa \
    --display-name="Vertex AI Service Account" \
    --project=project-1434b01c-dc17-4123-ba3
```

**Step 3.2: Grant Permissions**
Give the account the required `roles/aiplatform.user` role:
```bash
gcloud projects add-iam-policy-binding project-1434b01c-dc17-4123-ba3 \
    --member="serviceAccount:vertex-ai-sa@project-1434b01c-dc17-4123-ba3.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"
```

---

## 4. Overcoming the "Secure by Default" JSON Key Block

Google Workspace automatically locks down Organization security, preventing even Project Owners from generating Service Account JSON keys. This policy is called `constraints/iam.disableServiceAccountKeyCreation`.

**The Symptom (Console "View Policy" Issue):** 
If your `gcloud iam service-accounts keys create` command fails with a **FAILED_PRECONDITION** error, you might try to go to the Google Cloud Console -> Organization Policies to turn it off. However, you might find that you only see a **"View policy"** option without an "Edit" or "Manage" button. 

This happens because being a "Project Owner" or even an "Organization Admin" does not automatically grant you the rights to edit security policies. You specifically need the `roles/orgpolicy.policyAdmin` role.

Follow these exact terminal commands to investigate and fix this issue:

**Step 4.1: Identify your Organization ID and Active Account**
First, find out the Organization ID your project belongs to, and verify your current logged-in email.
```bash
# Get your active account email
gcloud config get-value account

# Find the parent Organization ID of your project
gcloud projects describe project-1434b01c-dc17-4123-ba3
# Look for the 'parent.id' value in the output (e.g., 396743985220)
```

**Step 4.2: Verify your Current Organization Permissions (Optional)**
You can verify why you are blocked in the console by listing the IAM policy of the organization and seeing that `roles/orgpolicy.policyAdmin` is missing for your email:
```bash
gcloud organizations get-iam-policy 396743985220
```

**Step 4.3: Grant Yourself Organization Policy Admin Role**
Elevate your own user account so you have the authority to edit the policy. Replace the email and Org ID with yours. *(Note: You must be an Organization Admin/Owner at the top level to assign this role to yourself).*
```bash
gcloud organizations add-iam-policy-binding 396743985220 \
    --member="user:YOUR_EMAIL@gmail.com" \
    --role="roles/orgpolicy.policyAdmin"
```

**Step 4.4: Disable the Blocking Constraint**
Now that you have the correct permissions, use the CLI to turn off the security lock at both the Organization and Project levels:
```bash
# Disable at Org Level
gcloud resource-manager org-policies disable-enforce constraints/iam.disableServiceAccountKeyCreation --organization=396743985220

# Disable at Project Level
gcloud resource-manager org-policies disable-enforce constraints/iam.disableServiceAccountKeyCreation --project=project-1434b01c-dc17-4123-ba3
```
*Note: Wait 2-3 minutes for the policy change to propagate across Google's global servers before proceeding.*

---

## 5. Download the Infinite JSON Key

Once the policy block is removed, generate your permanent `credentials.json` file:
```bash
gcloud iam service-accounts keys create credentials.json \
    --iam-account=vertex-ai-sa@project-1434b01c-dc17-4123-ba3.iam.gserviceaccount.com \
    --project=project-1434b01c-dc17-4123-ba3
```
* **Lifespan:** This JSON key will work indefinitely ("until infinity"). It never requires a manual refresh or re-login.
* **⚠️ MASSIVE SECURITY WARNING:** Do not **EVER** commit this file to Git. Malicious bots scrape GitHub for these JSON files to steal compute resources. **Immediately add `credentials.json` to your `.gitignore`.**

---

## 6. Using the JSON Key in Docker

This project uses LiteLLM as its LLM transport layer. LiteLLM reads `VERTEXAI_CREDENTIALS` (not `GOOGLE_APPLICATION_CREDENTIALS`) to locate the service account key file. When set, it passes the file path directly to `google.oauth2.service_account` — bypassing `google.auth.default()` entirely.

**Standard Docker Run:**
```bash
docker run -d \
  -e VERTEXAI_CREDENTIALS=/app/credentials.json \
  -v $(pwd)/credentials.json:/app/credentials.json:ro \
  my-docker-image
```

**Docker Compose (recommended approach):**

This project ships a dedicated override file for Vertex AI. Instead of editing `docker-compose.yml`, use the override file — no commenting/uncommenting required:

```bash
# Place credentials.json in the repo root, then:
docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d
```

`docker-compose.vertex.yml` mounts the credentials file and sets `VERTEXAI_CREDENTIALS` only when you explicitly include it. The base `docker-compose.yml` stays clean for users on other providers.

---

## 7. Python Implementation Example

When using the `google-genai` SDK, specify `vertexai=True` and provide the exact project IDs and geographic location. Do **not** hardcode passwords or paths here; the SDK will automatically pick up the credentials from the `VERTEXAI_CREDENTIALS` environment variable read by LiteLLM, or from `GOOGLE_APPLICATION_CREDENTIALS` if using the Google SDK directly.

```python
from google import genai

# Initialize the Vertex AI client
# Authentication is handled automatically via VERTEXAI_CREDENTIALS (LiteLLM) or ADC
client = genai.Client(
    vertexai=True,
    project="project-1434b01c-dc17-4123-ba3",
    location="asia-south1"  # "asia-south1" maps to Mumbai, India
)

try:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="Explain how Service Accounts work in Google Cloud.",
    )
    print(response.text)
except Exception as e:
    print(f"Vertex AI Error: {e}")
```