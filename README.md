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

You only need **two** things:

| Requirement | Get it |
|---|---|
| 🐳 **Docker Desktop** (running) | [Download](https://www.docker.com/products/docker-desktop/) — Linux: [Engine](https://docs.docker.com/engine/install/) + [Compose](https://docs.docker.com/compose/install/) |
| 🔑 **Google Vertex AI service account key** (`credentials.json`) | Created in step 2 below — full walkthrough in the [Vertex AI Setup Guide](docs/VERTEX_AI_SETUP_GUIDE.md) |

No Python. No Node.js. No manual dependency installs.

### 1. Clone the repo

```bash
git clone -b develop https://github.com/monkscode/Natural-Language-to-Robot-Framework.git
cd Natural-Language-to-Robot-Framework
```

### 2. Create your Vertex AI credentials

Mark 1 authenticates to Google Cloud Vertex AI with a **service account key** (`credentials.json`). The one-time setup script below creates it for you.

> 🧰 **Prerequisite — the [gcloud CLI](https://cloud.google.com/sdk/docs/install):** Google's command-line tool, which the script uses to talk to your Google Cloud account. If you don't have it yet, install it from the [official install page](https://cloud.google.com/sdk/docs/install) (pick your OS and follow the steps). Forgot to install it? No problem — the script detects that, shows you the link, and waits while you install instead of failing.


```bash
bash setup_google_vertexAI.sh
```

It logs you in (`gcloud init`), picks up your project ID automatically, enables the Vertex AI API, creates a `vertex-ai-sa` service account with the **Vertex AI User** role, and downloads its key as `credentials.json` into the repo root. The full manual walkthrough (including fixes for organization-policy blocks) is in the [Vertex AI Setup Guide](docs/VERTEX_AI_SETUP_GUIDE.md).

> ⚠️ **Never commit `credentials.json` to Git** — it is already listed in `.gitignore`. If key creation fails with `FAILED_PRECONDITION`, your organization blocks service-account keys; see [§4 of the setup guide](docs/VERTEX_AI_SETUP_GUIDE.md#4-overcoming-the-secure-by-default-json-key-block) for the fix.

### 3. Create the two config files

Mark 1 uses two `.env` files — one for **infrastructure** (which images to run) and one for **your settings** (Vertex AI credentials, model, toggles).

```bash
cp .env.example .env                              # root: Docker image tags
cp src/backend/.env.example src/backend/.env      # backend: Vertex AI + settings
```

In **`src/backend/.env`**, set the Vertex AI project and location — these are the **only** values you must change:

```env
MODEL_PROVIDER=vertex
VERTEXAI_PROJECT=your-project-id
VERTEXAI_LOCATION=us-central1
ONLINE_MODEL=gemini-2.5-flash
```

- **`VERTEXAI_PROJECT`** — the setup script prints this at the end (it is also the `project_id` field inside `credentials.json`).
- **`VERTEXAI_LOCATION`** — the Google Cloud region to serve Vertex AI requests from, e.g. `us-central1` or `asia-south1` (Mumbai).
- **`VERTEXAI_CREDENTIALS`** — already defaults to `credentials.json`; leave it as is. When running in Docker, `docker-compose.vertex.yml` rewrites it to the in-container path automatically.

Everything else has sensible defaults. The database, internal service URLs, and inter-container networking are wired up automatically by Docker Compose — you don't need to touch them.

### 4. Start everything

```bash
docker compose pull      # first time only — downloads the images (~2–5 min)
docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d
```

The extra `-f docker-compose.vertex.yml` mounts your `credentials.json` into the containers and tells the app where to find it (see [§6 of the setup guide](docs/VERTEX_AI_SETUP_GUIDE.md#6-using-the-json-key-in-docker)) — include it whenever you start the app.

Check that the services are healthy:

```bash
docker compose ps
```

> ⏳ The **browser service can take up to ~2 minutes** to report `healthy` on first start (it's initialising Playwright). This is normal.

Your generated tests, logs, and database live in local folders (`./robot_tests`, `./logs`, `./chroma_db`, `./data`) and are **preserved** across restarts. For everyday commands (`up`, `down`, `ps`, `logs`, `pull`), see the [Docker Compose CLI reference](https://docs.docker.com/reference/cli/docker/compose/).

### 5. Open the app & generate your first test

Open **[http://localhost:3000](http://localhost:3000)** and **create an account** — you'll land straight on the **Generate** page. Then:

1. Enter a description:
   ```
   Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project
   ```
2. Click **Generate & Run** and watch the agents work. ✨
3. View the generated `.robot` code, live progress, and the HTML report right in the UI.

> **Pro tip:** Be specific about elements — "first product name" or "search button in the header" beats vague phrasing. Another query to try: `Go to Wikipedia and search for Agentic AI`.

---

## 🧠 How It Works

Mark 1 uses a **multi-agent AI system**: your plain-English request is broken into precise steps, a live headless browser detects the real page elements, and the agents generate validated, production-ready Robot Framework code that runs in a clean, throwaway Docker container. You get working test code, detailed HTML reports with step-by-step logs, real-time progress, and locators that hold up on dynamic sites.

Want the deep dive? See the [Architecture Documentation](docs/ARCHITECTURE.md).

### Example output

**Input:**
```
Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project
```

**Generated test (Browser Library):**
```robot
*** Settings ***
Library    Browser
Library    BuiltIn

*** Variables ***
${browser}    chromium
${headless}    True
${url}    https://github.com/monkscode
${pinned_project_name_locator}    id=892238219

*** Test Cases ***
Generated Test
    [Documentation]    Auto-generated test case
    New Browser    ${browser}    headless=${headless}
    New Context    viewport=None
    New Page    ${url}
    ${pinned_project_name}=    Get Text    ${pinned_project_name_locator}
    Log    Retrieved Pinned project name: ${pinned_project_name}
    Close Browser
```

---

> 🔒 **Deploying to production?** The defaults are tuned for local use. Before exposing Mark 1 publicly, set a strong `JWT_SECRET_KEY`, `COOKIE_SECURE=true`, and `ENVIRONMENT=production` in `src/backend/.env`. See the [Configuration Guide](docs/CONFIGURATION.md) for the full hardening checklist.


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
