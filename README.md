<div align="center">

# 🤖 Mark 1 — Natural Language to Robot Framework

![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![Docker](https://img.shields.io/badge/Run%20with-Docker-2496ED.svg?logo=docker&logoColor=white)
![Robot Framework](https://img.shields.io/badge/Robot%20Framework-6.0%2B-orange.svg)
![AI Powered](https://img.shields.io/badge/AI-Powered-purple.svg)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)

**Turn plain English into production-ready test automation.**
Describe what you want to test, and Mark 1's AI agents generate a working
[Robot Framework](https://robotframework.org/) test for you — no coding required.

[Quick Start](#-quick-start-docker) • [How It Works](#-how-it-works) • [Configuration](docs/CONFIGURATION.md) • [Troubleshooting](#-troubleshooting) • [Docs](#-documentation)

</div>

---

> 🐳 **Mark 1 runs entirely in Docker.** No Python, Node, or pip installs — just Docker.

<!-- 💡 Maintainers: drop a short demo GIF or screenshot of the Generate page here — it's the single biggest boost to a project README. -->

---

## ✨ Why Mark 1?

- 🗣️ **No coding required** — write tests in plain English, not Python or JavaScript.
- 🌐 **Works on any website** — e-commerce, SaaS, internal tools, anything with a UI.
- ⚡ **Fast** — go from idea to a working test in ~20–30 seconds.
- ♻️ **Write once, run infinitely** — reuse generated tests across dev, staging, and prod.
- 🧠 **Gets smarter over time** — learns your app's patterns and uses fewer tokens per test.
- 👀 **Readable output** — clean Robot Framework code that even manual QAs can maintain.

---

## 🚀 Quick Start (Docker)

You need **three** things — all free, and none of them Python or Node:

| Requirement | Get it |
|---|---|
| 🐳 **Docker Desktop** (running) | [Download](https://www.docker.com/products/docker-desktop/) — Linux: [Engine](https://docs.docker.com/engine/install/) + [Compose](https://docs.docker.com/compose/install/) |
| ☁️ **gcloud CLI** | [Install](https://cloud.google.com/sdk/docs/install) — used once, in step 2, to mint your key. The setup script waits for you if it isn't installed yet. |
| 🔑 **Google Vertex AI service account key** (`credentials.json`) | Created in step 2 below — full walkthrough in the [Vertex AI Setup Guide](docs/VERTEX_AI_SETUP_GUIDE.md) |

No Python. No Node.js. No manual dependency installs.

> 🪟 **On Windows, run the commands in this guide from Git Bash**, not PowerShell or
> CMD. The setup script is a bash script, and `cp` is not a Windows command. Git Bash
> ships with [Git for Windows](https://gitforwindows.org/). WSL works too.

⏱️ **Budget ~15 minutes end to end**, most of it image downloads that run unattended.

### 1. Clone the repo

```bash
git clone -b develop https://github.com/monkscode/Natural-Language-to-Robot-Framework.git
cd Natural-Language-to-Robot-Framework
```

### 2. Create your Vertex AI credentials

For local development, Mark 1 authenticates to Google Cloud Vertex AI with a **service account key** (`credentials.json`). The one-time setup script below creates it for you.

> ⚠️ **This JSON-key workflow is for local development only.** A downloaded service-account key is a long-lived credential sitting on disk, and `.gitignore` only protects you from committing it — not from host compromise or exfiltration from a container. For production, authenticate without a key file: [Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation), an attached service account / [ADC](https://cloud.google.com/docs/authentication/application-default-credentials) on Cloud Run, GKE or GCE, or short-lived credentials injected by your deployment's secret manager.

> 🧰 **Prerequisite — the [gcloud CLI](https://cloud.google.com/sdk/docs/install):** Google's command-line tool, which the script uses to talk to your Google Cloud account. If you don't have it yet, install it from the [official install page](https://cloud.google.com/sdk/docs/install) (pick your OS and follow the steps). Forgot to install it? No problem — the script detects that, shows you the link, and waits while you install instead of failing.


```bash
bash setup_google_vertexAI.sh
```

It logs you in (`gcloud init`), picks up your project ID automatically, enables the Vertex AI API, creates a `vertex-ai-sa` service account with the **Vertex AI User** role, and downloads its key as `credentials.json` into the repo root. The full manual walkthrough (including fixes for organization-policy blocks) is in the [Vertex AI Setup Guide](docs/VERTEX_AI_SETUP_GUIDE.md).

> ⚠️ **Never commit `credentials.json` to Git** — it is already listed in `.gitignore`. If key creation fails with `FAILED_PRECONDITION`, your organization blocks service-account keys; see [§4 of the setup guide](docs/VERTEX_AI_SETUP_GUIDE.md#4-overcoming-the-secure-by-default-json-key-block) for the fix.
>
> **Rotating or revoking the key** (do this immediately if it ever leaks):
>
> ```bash
> SA_EMAIL="vertex-ai-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com"
> gcloud iam service-accounts keys list --iam-account="$SA_EMAIL" --managed-by=user
> gcloud iam service-accounts keys delete KEY_ID --iam-account="$SA_EMAIL"
> ```
>
> Deleting the key disables it in IAM within minutes. Delete the local `credentials.json` too, then re-run the setup script to mint a replacement. Re-running the script on its own reuses a still-valid key rather than piling up new ones.

### 3. Create the two config files

Mark 1 uses two `.env` files — one for **infrastructure** (which images to run) and one for **your settings** (Vertex AI credentials, model, toggles).

```bash
cp .env.example .env                              # root: Docker image tags
cp src/backend/.env.example src/backend/.env      # backend: Vertex AI + settings
```

In the **root `.env`**, point all four image tags at `-develop`. The file ships with
`-local` defaults, which are **not** published to Docker Hub — only `frontend` can be
built locally, so leaving them as-is makes `docker compose pull` fail:

```env
FASTAPI_IMAGE_TAG=monkscode/nlrf:fastapi-develop
BROWSER_SERVICE_IMAGE_TAG=monkscode/nlrf:browser-service-develop
TEST_RUNNER_IMAGE_TAG=monkscode/nlrf:test-runner-develop
FRONTEND_IMAGE_TAG=monkscode/nlrf:frontend-develop
```

All four must carry the **same** suffix — a mixed set is the usual cause of odd boot
and login errors after an update.

In **`src/backend/.env`**, set the Vertex AI project and location, and add your own
email as an admin:

```env
MODEL_PROVIDER=vertex
VERTEXAI_PROJECT=your-project-id
VERTEXAI_LOCATION=us-central1
ONLINE_MODEL=gemini-2.5-flash
ADMIN_EMAILS=you@example.com
```

Those three — project, location and admin email — are the only values you must change;
everything below is background on what they mean.

- **`JWT_SECRET_KEY`** — leave it alone. It ships commented out, and on first start the
  app generates a strong random secret and stores it in `data/jwt_secret`, so you stay
  signed in across restarts. Delete that file and everyone is signed out. Production is
  different — see the hardening note at the end of this section.

- **`ADMIN_EMAILS`** — set this **before the first start**. Mark 1 is approval-gated:
  a new signup lands `pending` and sees an Access Gate rather than the Generate page.
  Emails listed here are created `active` and can approve everyone else. Miss it and
  you lock yourself out of your own instance with no one able to let you in.

- **`VERTEXAI_PROJECT`** — the setup script prints this at the end (it is also the `project_id` field inside `credentials.json`).
- **`VERTEXAI_LOCATION`** — the Google Cloud region to serve Vertex AI requests from, e.g. `us-central1` or `asia-south1` (Mumbai).
- **`VERTEXAI_CREDENTIALS`** — already defaults to `credentials.json`; leave it as is. When running in Docker, `docker-compose.vertex.yml` rewrites it to the in-container path automatically.

Everything else has sensible defaults. The database, internal service URLs, and inter-container networking are wired up automatically by Docker Compose — you don't need to touch them.

### 4. Start everything

```bash
docker compose pull                                   # the services (~2–5 min)
docker pull monkscode/nlrf:test-runner-develop        # the test runner (~0.5 GB)
docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d
```

The extra `-f docker-compose.vertex.yml` mounts your `credentials.json` into the containers and tells the app where to find it (see [§6 of the setup guide](docs/VERTEX_AI_SETUP_GUIDE.md#6-using-the-json-key-in-docker)) — include it whenever you start the app.

> 📦 **Why the second `docker pull`?** Your tests execute in a throwaway `test-runner`
> container that the app launches on demand, so it is deliberately not a Compose
> service and `docker compose pull` does not fetch it. It is ~0.5 GB compressed
> (~1.9 GB on disk) and takes about 99 seconds.
>
> Pulling it here is belt-and-braces: the app also fetches it in the background as
> soon as it starts, so it is normally ready by the time you finish signing up and
> writing your first test. Doing it now just guarantees that. Keep the tag suffix
> the same as the four in your root `.env`.

Check that the services are healthy:

```bash
docker compose ps
```

> ⏳ The **browser service can take up to ~2 minutes** to report `healthy` on first start (it's initialising Playwright). This is normal — its healthcheck grants a 120s `start_period` for exactly this reason.

Your generated tests, logs, and database live in local folders (`./robot_tests`, `./logs`, `./data`) and are **preserved** across restarts. For everyday commands (`up`, `down`, `ps`, `logs`, `pull`), see the [Docker Compose CLI reference](https://docs.docker.com/reference/cli/docker/compose/).

### 5. Open the app & generate your first test

Open **[http://localhost:3000](http://localhost:3000)** and **create an account**, using the
email you put in `ADMIN_EMAILS`. That account is active immediately and lands on the
**Generate** page; any other email lands on the Access Gate until an admin approves it.
Then:

1. Enter a description:
   ```text
   Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project
   ```
2. Click **Generate Test** and watch the agents work — planning, finding the real
   elements on the live page, then writing the code. Takes ~20–30 seconds. ✨
3. The generated `.robot` code appears on the right, and it's editable. Click
   **Run Test** to execute it in a clean container.
4. When it finishes, open **View Report** or **Detailed Log** for the full
   step-by-step HTML report.

> **Generating and running are two separate clicks.** That's deliberate — you get to
> read (and edit) the generated code before anything executes. Already have a `.robot`
> file? Paste it straight into the code panel and click **Run Test**; no description needed.

> **Pro tip:** Be specific about elements — "first product name" or "search button in the header" beats vague phrasing. Another query to try: `Go to Wikipedia and search for Agentic AI`.

#### Sites with a one-time popup after login

Some sites show an announcement or welcome popup exactly once per login, on the first page that finishes rendering. Add a short wait right after the login step so the popup appears and expires on the landing page — before your real steps run — instead of blocking a click later in the test:

```text
Go to https://yourapp.example.com, type admin in the username field, type admin in the password field, click the Sign In button, wait 5 seconds for the dashboard to load, go to the reports page, and click the Filter button
```

Two rules: put the wait immediately after login, and make sure a navigation to another page follows it. Persistent popups (cookie banners, consent dialogs) don't need this trick — just mention them as a step ("accept the cookie banner") and they are automated like any other click.

---

## 🧠 How It Works

Mark 1 uses **AI agents combined with deterministic validation**: your plain-English request is broken into precise steps, a live headless browser detects the real page elements, and the agents generate Robot Framework code that is then gated by a deterministic `robot --dryrun` check before it runs in a clean, throwaway Docker container. You get working test code, detailed HTML reports with step-by-step logs, real-time progress, and locators that hold up on dynamic sites.

Want the deep dive? See the [Architecture Documentation](docs/ARCHITECTURE.md).

### Example output

**Input:**
```text
Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project
```

**Generated test (Browser Library)** — a real, unedited run:
```robot
*** Settings ***
Library    Browser    timeout=30s
Library    BuiltIn
Library    Collections

*** Variables ***
${browser}    chromium
${headless}    True
${pinned_project_name_locator}    id=880667900

*** Test Cases ***
Generated Test Case
    New Browser    ${browser}    headless=${headless}
    New Context    viewport={'width': 1920, 'height': 1080}
    New Page    https://github.com/monkscode
    ${pinned_project_name}=    Get Text    ${pinned_project_name_locator}
    Close Browser
```

The retrieved value is recorded in the run's `log.html` — Robot Framework logs every
variable assignment, so the report for this test shows
`${pinned_project_name} = ML-practice` under the `Get Text` step. Exact locators and
variable names vary per run, because they are discovered on the live page.

---

> 🔒 **Deploying to production?** The defaults are tuned for local use. Before exposing Mark 1 publicly:
>
> - Set `ENVIRONMENT=production` and `COOKIE_SECURE=true` in `src/backend/.env`, and set `JWT_SECRET_KEY` **explicitly**. Production never uses the auto-generated `data/jwt_secret` — inject the secret from your environment or secret manager, or the app refuses to start. To keep everyone signed in across the switch, copy the value out of `data/jwt_secret` first.
> - **Drop the `credentials.json` key file.** The Quick Start's JSON key is a local-development shortcut. In production use [Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation), an attached service account / ADC (Cloud Run, GKE, GCE), or short-lived credentials injected by your secret manager — none of which leave a long-lived key on disk.
> - If you must ship a key, scope it to `roles/aiplatform.user` only, mount it read-only, and put it on a rotation schedule with a documented revocation path (see step 2 above).
>
> See the [Configuration Guide](docs/CONFIGURATION.md) for the full hardening checklist.


---

## 🐛 Troubleshooting

Hit a snag? Common symptoms and their fixes are collected in the [Troubleshooting Guide](docs/TROUBLESHOOTING.md), with more Docker-specific help in the [Docker Guide → Troubleshooting](docs/DOCKER-GUIDE.md#troubleshooting).


---

## 📚 Documentation

- 🐳 **[Docker Guide](docs/DOCKER-GUIDE.md)** — run Mark 1 with Docker
- 🔐 **[Vertex AI Setup Guide](docs/VERTEX_AI_SETUP_GUIDE.md)** — create the service account & `credentials.json`
- ⚙️ **[Configuration Guide](docs/CONFIGURATION.md)** — every setting explained
- 🏗️ **[Architecture](docs/ARCHITECTURE.md)** — how Mark 1 works under the hood
- 🐛 **[Troubleshooting](docs/TROUBLESHOOTING.md)** · ❓ **[FAQ](docs/FAQ.md)** · ✅ **[Best Practices](docs/BEST_PRACTICES.md)**
- 🤝 **[Contributing](CONTRIBUTING.md)**

---

## 🤝 Contributing & Support

Contributions are welcome — bug fixes, features, or docs. See the [Contributing Guide](CONTRIBUTING.md) for how to set up your environment and open a PR; by submitting one you agree to our [Contributor License Agreement](CLA.md). For bugs and feature requests, open a [GitHub Issue](https://github.com/monkscode/Natural-Language-to-Robot-Framework/issues); for questions, use [GitHub Discussions](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions).

## 📄 License

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE).

---

<div align="center">
  <b>⭐ If Mark 1 saves you time, star the repo and share it!</b><br/>
  Built with ❤️ for the test automation community
  <br/><br/>
  <sub>Mark 1 is not affiliated with or endorsed by Google, Robot Framework, or any mentioned websites. All trademarks belong to their respective owners.</sub>
</div>
