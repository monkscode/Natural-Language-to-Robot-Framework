# Configuration Guide

This guide covers all configuration options for Mark 1.

## Environment Variables

Mark 1 is configured using environment variables in the `.env` file located at `src/backend/.env`.

### Quick Setup

```bash
# Copy the example file
cp src/backend/.env.example src/backend/.env

# Edit with your settings
nano src/backend/.env  # or use your preferred editor
```

## Core Settings

### MODEL_PROVIDER

Controls which AI provider to use.

```env
MODEL_PROVIDER=gemini  # or 'vertex' or 'local'
```

**Options:**
- `gemini` - Google AI Studio (requires `GEMINI_API_KEY`)
- `vertex` - Google Cloud Vertex AI (requires service account credentials)
- `local` - Locally hosted models via Ollama

**Recommendation:** Use `gemini` for development, `vertex` for production/Docker.

### GEMINI_API_KEY

Your Google Gemini API key (required for `MODEL_PROVIDER=gemini`).

```env
GEMINI_API_KEY=your-actual-api-key-here
```

**How to get:**
1. Visit [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Sign in with Google account
3. Click "Create API Key"
4. Copy and paste into `.env`

**Free Tier:** 1,500 requests per day

### ONLINE_MODEL

Which model to use (bare name — the provider prefix is added automatically based on `MODEL_PROVIDER`).

```env
ONLINE_MODEL=gemini-3.5-flash
```

**Default:** `gemini-3.5-flash`

**Available Models:**
- `gemini-3.5-flash` - the default, and the only one with benchmark evidence
- `gemini-2.5-flash` - cheaper, fewer thinking tokens; never benched here
- `gemini-2.0-flash` - faster, slightly less capable
- `gemini-1.5-pro` - more powerful, slower

**Recommendation:** keep `gemini-3.5-flash`. Every baseline in `bench/baselines/`
was measured on it, so the pass rate, cost per run and timings this project
publishes describe that model and no other. The alternatives work; they simply
have no numbers behind them.

It is not the cheap option, and the gap is worth knowing before you change it.
Per LiteLLM's own price map, `gemini-3.5-flash` on Vertex costs **$1.50 per
million input tokens and $9.00 per million output**, against `gemini-2.5-flash`
at **$0.30 and $2.50** — 5x input, 3.6x output — before counting the extra
thinking tokens 3.5 generates by default. Switching to `2.5-flash` is a
legitimate way to cut cost; you are trading away the only configuration with
measured reliability behind it.

### LOCAL_MODEL

Which Ollama model to use (for local mode).

```env
LOCAL_MODEL=llama3.1
```

**Available Models:**
- `llama3.1` - Good balance
- `llama3` - Faster, less accurate
- `mistral` - Alternative option

**Note:** Model must be pulled first:
```bash
ollama pull llama3.1
```

## Application Settings

### APP_PORT

Port for the FastAPI backend.

```env
APP_PORT=5000
```

**Default:** 5000

**Change if:** Port 5000 is already in use on your system.

> **Note:** the bind address is not configurable through `.env`. Under Docker
> Compose the API is published on loopback only (`127.0.0.1:5000`) and end users
> reach it through the nginx frontend on `:3000`. Change the `ports:` mapping in
> `docker-compose.yml` if you need something different.

## Browser Automation Settings

### BROWSER_USE_SERVICE_URL

URL for the BrowserUse AI service.

```env
BROWSER_USE_SERVICE_URL=http://127.0.0.1:4999
```

**Default:** `http://127.0.0.1:4999` (`core/config.py`)

Use `127.0.0.1`, not `localhost`. On Windows `localhost` resolves to `::1` first
and this service binds IPv4 only, so every call stalls ~2s before falling back.
Docker Compose overrides this to the `browser-service` service name.

**Change if:** Running the service on a different host/port.

### BROWSER_USE_TIMEOUT

Maximum time (seconds) for element detection.

```env
BROWSER_USE_TIMEOUT=900
```

**Default:** 900 seconds (15 minutes)

**Adjust based on:**
- Website complexity
- Network speed
- Test complexity

### ROBOT_LIBRARY

Which Robot Framework library the generated tests use.

```env
ROBOT_LIBRARY=browser
```

**`browser` is the only supported value.** Setting `selenium` makes the app **fail
at startup** with:

```
ROBOT_LIBRARY=selenium is no longer supported; this system generates
Browser Library (Playwright) tests only.
```

SeleniumLibrary support was removed: the locator pipeline validates and emits
Playwright-only syntax, so there is no code path that can produce a working
Selenium test. Leave this at `browser`, or omit it entirely.

**What you get with Browser Library:**
- Playwright engine — the *same* engine used to discover and validate the locators,
  so what was verified during generation is what runs
- Auto-waiting, so generated tests need far fewer explicit waits
- Text (`text=Login`) and role (`role=button[name="Submit"]`) locators alongside
  CSS and XPath
- Shadow DOM, iframes and SPA support

**Example output:**
```robot
*** Settings ***
Library    Browser    timeout=30s

*** Test Cases ***
Example Test
    New Browser    chromium    headless=True
    New Context    viewport={'width': 1920, 'height': 1080}
    New Page    https://example.com
    Fill Text    name=q    search term
    Click    text=Search
    Close Browser
```

## Logging Settings

### LOG_LEVEL — NOT WIRED UP

**Setting this has no effect.** `setup_logging()` in
`src/backend/config/logging_config.py` takes the level as a function argument
defaulting to `"INFO"`, and `src/backend/main.py:20` calls it with no arguments.
No code reads a `LOG_LEVEL` environment variable. To change the level you must
pass it at the call site.

`LOG_FORMAT=console` *is* read (`logging_config.py:170`) and does work.

The values below describe the intended levels, not a working switch.

```env
LOG_LEVEL=INFO   # inert
```

**Options:**
- `DEBUG` - Verbose logging (for troubleshooting)
- `INFO` - Standard logging (recommended)
- `WARNING` - Only warnings and errors
- `ERROR` - Only errors

### LOG_DIR — NOT WIRED UP

**Setting this has no effect.** No code reads a `LOG_DIR` environment variable —
`setup_logging()` takes the directory as a function argument and defaults to
`"logs"`. Inert in exactly the same way as `LOG_LEVEL` above.

(`BROWSER_USE_LOG_DIR` is a different variable and *is* read, by the browser
service — see `tools/browser_service/__init__.py`.)

```env
LOG_DIR=logs   # inert
```

## Docker Settings

Tests run in a throwaway `test-runner` container that the app launches per run. It
is **not** a Compose service, so `docker compose pull` does not fetch it — see the
README's step 4.

### TEST_RUNNER_IMAGE_TAG

Which runner image to run tests in. Set this in the **root `.env`** (next to
`docker-compose.yml`), not in `src/backend/.env`, because Compose reads that file
for `${VAR}` substitution.

```env
TEST_RUNNER_IMAGE_TAG=monkscode/nlrf:test-runner-develop
```

Keep the suffix the same as the other three image tags in that file. Pre-pull it
with `docker pull monkscode/nlrf:test-runner-develop` so your first run does not
wait on the download (~0.5 GB compressed, ~1.9 GB on disk).

### PREFER_REMOTE_DOCKER_IMAGE

Whether to pull the runner image before falling back to building it locally.

```env
PREFER_REMOTE_DOCKER_IMAGE=true
```

**Default when the variable is absent: `false`** (`services/docker_service.py`,
`os.getenv('PREFER_REMOTE_DOCKER_IMAGE', 'false')`).

**Set it explicitly to `true`.** Omitted, the fallback is `false`, which builds
the runner image from source on your first **Run Test** — minutes of CPU instead
of a ~0.5 GB pull — and also disables the startup warm-up below. The shipped
`src/backend/.env.example` sets it; only a hand-trimmed `.env` loses it.

The `false` fallback is deliberate: with no `TEST_RUNNER_IMAGE_TAG` configured
either, a `true` fallback would pull `monkscode/nlrf:test-runner-latest` — the
main-branch build, which is not the one this tree is developed against.

### REMOTE_DOCKER_IMAGE

Registry to pull the runner from. **Leave unset** unless you mirror the image
yourself: the pulled image is re-tagged locally as `TEST_RUNNER_IMAGE_TAG`, so
naming a different build here puts the wrong content under that tag. Unset, it
tracks `TEST_RUNNER_IMAGE_TAG` automatically.

### RUNNER_IMAGE_WARMUP

Pre-fetch the runner image when the executor starts, rather than on the first
**Run Test**.

```env
RUNNER_IMAGE_WARMUP=true
```

**Default:** `true`. The download is ~0.5 GB and takes about 99 seconds; starting
it at boot overlaps it with signup, writing your first query and the ~23s
generation stage, so by the time you click **Run Test** the image is normally
already there. It runs on a background thread, never delays the service becoming
healthy, and is pull-only — it will not start a local image build. If it fails,
the image is provisioned on first use exactly as before.

Set to `false` on metered connections or air-gapped hosts.

### TEST_EXECUTION_TIMEOUT

Maximum seconds to wait for a test container to finish. Set in the **root `.env`**.

```env
TEST_EXECUTION_TIMEOUT=1800
```

**Default:** 1800 (30 minutes). Increase for very long suites.

## Advanced Settings

### CREWAI_VERBOSE

Enable verbose output from CrewAI agents.

```env
CREWAI_VERBOSE=true
```

**Default:** `false`

**Options:**
- `true` - Show detailed agent workflow
- `false` - Minimal output

### MAX_AGENT_ITERATIONS

Maximum retry iterations for an agent task.

```env
MAX_AGENT_ITERATIONS=3
```

**Default:** 3. **Valid range: 1–5** — values outside it are rejected at startup.

### MAX_CONCURRENT_WORKFLOWS

How many generation/execution workflows may run at once.

```env
MAX_CONCURRENT_WORKFLOWS=10
```

**Default:** 10. **Valid range: 1–50.** Each workflow spawns a CrewAI thread and may
launch a test container, so lower this on memory-constrained hosts.

### MAX_LOCATOR_STRATEGIES

How many locator strategies to try when resolving an element.

```env
MAX_LOCATOR_STRATEGIES=21
```

**Default:** 21. **Valid range: 1–50.**

> **Model temperature is not configurable.** It is pinned low in code deliberately —
> test generation wants determinism, and exposing it invites irreproducible runs.

## Authentication & Database

Mark 1 is multi-user and approval-gated. These are the settings a first-time
operator actually has to think about.

### ADMIN_EMAILS

Comma-separated emails that receive the `admin` role at signup.

```env
ADMIN_EMAILS=you@company.com,cofounder@company.com
```

**Default:** empty.

> ⚠️ **Set this before your first start.** Every new signup lands `pending` and sees
> an Access Gate instead of the Generate page. Accounts listed here are created
> `active` and can approve everyone else. Miss it and nobody can get in — including
> you. Admins approve others under **Access** in the sidebar; there is no email
> notification, so check that page after a teammate signs up.

### JWT_SECRET_KEY

Secret used to sign access tokens.

```env
# leave unset for local development
#JWT_SECRET_KEY=
```

In **development** (the default), leave it unset: the app generates a strong random
secret on first start and persists it to `data/jwt_secret`, so logins survive
restarts. Delete that file and everyone is signed out.

With `ENVIRONMENT=production` nothing is generated — the app **refuses to start**
unless this is set explicitly, is at least 32 characters, and is not a placeholder.
Generate one with:

```bash
docker run --rm monkscode/nlrf:fastapi-develop python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Running outside Compose (plain `docker run`, Kubernetes, more than one replica)
without persisting `/app/data` puts the generated secret on a throwaway layer, so
each restart and each replica signs with a different key. Set it explicitly there.

### ENVIRONMENT / COOKIE_SECURE

```env
ENVIRONMENT=development   # 'production' enables the strict startup checks
COOKIE_SECURE=false       # set true when serving over HTTPS
```

### AUTH_ENFORCED

```env
AUTH_ENFORCED=true
```

**Default:** `true`. Setting `false` lets token-less requests through — an escape
hatch for local API-only debugging and for the bench. **Never disable it in
production.**

### DATABASE_URL

PostgreSQL connection string. Compose wires this up for you.

```env
DATABASE_URL=postgresql://nlrf:nlrf@postgres:5432/nlrf   # inside Compose
```

Outside Compose the host is `localhost` instead of the `postgres` service name.

### Google SSO (optional)

Leave `GOOGLE_CLIENT_ID` empty to disable the "Continue with Google" button.

```env
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:5000/auth/google/callback
```

### Production hardening checklist

1. `ENVIRONMENT=production`, `COOKIE_SECURE=true`, `JWT_SECRET_KEY` injected from a
   secret manager.
2. Drop `credentials.json` — use [Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation),
   an attached service account/ADC, or short-lived injected credentials.
3. If you must ship a key, scope it to `roles/aiplatform.user`, mount it read-only,
   and give it a rotation schedule.
4. Keep `AUTH_ENFORCED=true` and set `ALLOWED_ORIGINS` to your real front-end origin.
5. Put the app behind HTTPS — the API binds loopback under Compose and expects the
   frontend proxy in front of it.

## Example Configurations

### Docker Quick Start (Vertex AI) — what the README sets up

```env
# AI Provider
MODEL_PROVIDER=vertex
VERTEXAI_PROJECT=your-gcp-project-id
VERTEXAI_LOCATION=us-central1
ONLINE_MODEL=gemini-3.5-flash

# Access — set BEFORE the first start
ADMIN_EMAILS=you@company.com

# JWT_SECRET_KEY intentionally unset: auto-generated into data/jwt_secret

ROBOT_LIBRARY=browser
LOG_LEVEL=INFO
```

Start it with the Vertex overlay so `credentials.json` is mounted:
`docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d`

### Google AI Studio key instead of a service account

Both services also accept a plain AI Studio API key, which skips the gcloud CLI,
the GCP project and the `credentials.json` mount — the Vertex compose overlay is
not needed at all. The free tier is rate-limited, so it suits a first look rather
than sustained use; the README's Vertex path is what the project benchmarks and
runs in production.

```env
MODEL_PROVIDER=gemini
GEMINI_API_KEY=your-ai-studio-key   # https://aistudio.google.com/apikey
ONLINE_MODEL=gemini-3.5-flash
ADMIN_EMAILS=you@company.com
```

Start it without the overlay: `docker compose up -d`

### Fully local (Ollama)

`local` drives the backend LLM only. The browser service requires a Google vision
model and does **not** support Ollama, so element detection still needs
`GEMINI_API_KEY` or Vertex credentials.

```env
MODEL_PROVIDER=local
LOCAL_MODEL=llama3
OLLAMA_API_BASE=http://host.docker.internal:11434   # Docker Desktop

ROBOT_LIBRARY=browser
LOG_LEVEL=DEBUG
CREWAI_VERBOSE=true
```

## Configuration Validation

Mark 1 validates configuration on startup. Common validation errors:

### "GEMINI_API_KEY not found"
- Add key to `.env` file
- Ensure no extra spaces

### "ROBOT_LIBRARY=selenium is no longer supported"
- `browser` is the only accepted value (lowercase); remove the setting or set it to `browser`
- SeleniumLibrary support was removed — the locator pipeline emits Playwright-only syntax

### "Invalid MODEL_PROVIDER"
- Must be `gemini`, `vertex`, or `local` (lowercase)
- The old value `online` is no longer accepted — use `gemini` instead
- Check spelling

## Environment-Specific Configuration

### Using Multiple Environments

Create separate `.env` files:

```bash
.env.development
.env.production
.env.testing
```

Load the appropriate one:

```bash
# Development
cp .env.development src/backend/.env

# Production
cp .env.production src/backend/.env
```

### CI/CD Configuration

Set environment variables in your CI/CD platform:

**GitHub Actions:**
```yaml
env:
  MODEL_PROVIDER: gemini
  GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
  ONLINE_MODEL: gemini-2.5-flash
```

**GitLab CI:**
```yaml
variables:
  MODEL_PROVIDER: "gemini"
  ONLINE_MODEL: "gemini-2.5-flash"
```

## Security Best Practices

1. **Never commit `.env` files** - Already in `.gitignore`
2. **Use secrets management** - For production deployments
3. **Rotate API keys** - Regularly update keys
4. **Limit key permissions** - Use least privilege
5. **Use local models** - For sensitive data

## Troubleshooting Configuration

### Configuration not loading

1. Check file location: `src/backend/.env`
2. Verify file name (not `.env.txt`)
3. Check file permissions
4. Restart the application

### Values not taking effect

1. Restart all services
2. Check for typos in variable names
3. Verify no extra spaces or quotes
4. Check logs for validation errors

## Getting Help

- Configuration issues? Check [Troubleshooting Guide](TROUBLESHOOTING.md)
- Questions? Open a [GitHub Discussion](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions)
- Found a bug? Report on [GitHub Issues](https://github.com/monkscode/Natural-Language-to-Robot-Framework/issues)
