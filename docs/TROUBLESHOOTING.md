# Troubleshooting Guide

This guide helps you resolve common issues with Mark 1.

## Quick Fixes (Docker Quick Start)

| Symptom | Fix |
|---|---|
| **App won't load on `:3000`** | Find what holds the port — `netstat -ano \| findstr :3000` on Windows (`lsof -i :3000` on macOS/Linux) — then stop that process, or leave it alone and remap Mark 1 instead: change the frontend's `ports:` entry in `docker-compose.yml` from `"3000:8080"` to e.g. `"3001:8080"` and open `:3001`. |
| **A container never becomes `healthy`** | Check logs: `docker compose logs fastapi` / `docker compose logs browser-service`. |
| **First **Run Test** fails with `runner-exec unreachable: ... Read timed out`** | Your tests execute in a `test-runner` container that is pulled on demand, and it is ~0.5 GB compressed (~1.9 GB on disk) — the first run waits on that download, measured at 99s. Pull it up front so this cannot happen: `docker pull monkscode/nlrf:test-runner-develop` (match the tag suffix in your root `.env`). If you already hit it, the download is still finishing in the background — wait for it to complete and click **Run Test** again. Watch progress with `docker compose logs -f runner-exec`. |
| **Generation fails and the message names a setting** (e.g. `GEMINI_API_KEY`, `credentials.json`, billing, Vertex AI API) | That message is the fix — apply it, then `docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d` to reload. Settings live in `src/backend/.env`. |
| **`fastapi` exits immediately, log says `JWT_SECRET_KEY is unset or still the placeholder`** | Only happens with `ENVIRONMENT=production`, which never auto-generates a secret. Generate one — `docker run --rm monkscode/nlrf:fastapi-develop python -c "import secrets; print(secrets.token_urlsafe(48))"` — and inject it as `JWT_SECRET_KEY` via the container environment or your secret manager. To keep existing sessions valid, copy the value out of `data/jwt_secret` instead. |
| **Everyone is signed out after a restart, log warns the secret "could not persist"** | The app could not write `data/jwt_secret`, so it mints a new one each boot — usually `data/` being owned by another user on Linux. Fix the ownership (`chown 1000:1000 data`) or set `JWT_SECRET_KEY` explicitly in `src/backend/.env`. |
| **Signed out after every restart, or `401` on some requests but not others, outside Compose** | Running the image without persisting `/app/data` (plain `docker run`, Kubernetes, more than one replica) puts the generated secret on the container's throwaway layer, so each restart and each replica signs with a different key. `docker compose` mounts `./data` for you; anywhere else, set `JWT_SECRET_KEY` explicitly. |
| **`Conflict. The container name "/nlrf-postgres" is already in use`** | You have run the source stack (`./run.sh`) before; it leaves that container running and Compose uses fixed container names. Move it aside with `docker rename nlrf-postgres nlrf-postgres-devstack` (reversible, and it keeps the container). Note that stopping it is **not** enough — a stopped container still holds the name. The two stacks keep separate data volumes either way. |
| **`401` / `403` / authentication errors** | Check `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` in `src/backend/.env`, make sure `credentials.json` is in the repo root, and start with the `docker-compose.vertex.yml` override. |
| **`address already in use`** | Another app holds the port — stop it, or edit the port mapping in `docker-compose.yml`. |
| **Browser service slow to start** | Expected on first start — it downloads browser binaries. Its healthcheck allows a 120s grace period (`start_period` in `docker-compose.yml`), so give it ~2 minutes before treating it as stuck. |
| **Boot/login errors after an update** | Make sure all four image tags use the **same** `-develop` suffix, then re-run `docker compose pull` followed by `docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d`. |
| **`429`, `RESOURCE_EXHAUSTED`, or "quota exceeded"** | You are over your Vertex AI / Gemini rate limit. Wait and retry; if it is persistent, raise the quota for the model in the GCP console (IAM & Admin → Quotas, filter on `aiplatform.googleapis.com`). Note that this also distorts run **timing** — check the log window for 429s before trusting any latency number. |
| **`403` / `PermissionDenied` even though credentials load** | The credentials are valid but the service account lacks the role. Grant it **Vertex AI User** (`roles/aiplatform.user`) on the project in `VERTEXAI_PROJECT`. Authentication failing (`401`) is a different problem — that one is the credentials path. |
| **`Model not found` / `404` naming the model** | `ONLINE_MODEL` in `src/backend/.env` does not exist in the region set by `VERTEXAI_LOCATION`. Model availability is per-region — check the model is offered in that region, or move `VERTEXAI_LOCATION` to one where it is. |
| **`Context length exceeded` / `maximum tokens`** | The prompt outgrew the model's context window. Usually a very large page or a very long query — simplify the query, or switch `ONLINE_MODEL` to a larger-context model. |

More Docker-specific fixes: [Docker Guide → Troubleshooting](DOCKER-GUIDE.md#troubleshooting).

## Installation Issues

### "Docker is not available"

**Symptoms:**
- Error message when starting Mark 1
- Tests fail to execute

**Solutions:**
1. Make sure Docker Desktop is running
2. Check the system tray (Windows) or menu bar (Mac) for Docker icon
3. Verify Docker is working:
   ```bash
   docker --version
   docker ps
   ```
4. Restart Docker Desktop if needed

### "GEMINI_API_KEY not found"

**Symptoms:**
- Error on startup
- API calls fail

**Solutions:**
1. Verify `.env` file exists in `src/backend/`
2. Check the API key is on the correct line:
   ```env
   GEMINI_API_KEY=your-actual-key-here
   ```
3. No extra spaces or quotes around the key
4. File is named `.env` not `.env.txt`

### "Port 5000 already in use"

**Symptoms:**
- Backend fails to start
- Port conflict error

**Solutions:**
1. Change the port in `.env`:
   ```env
   APP_PORT=8000
   ```
2. Or kill the process using port 5000:
   ```bash
   # Linux/Mac
   lsof -i :5000
   kill -9 <PID>
   
   # Windows
   netstat -ano | findstr :5000
   taskkill /PID <PID> /F
   ```

### "BrowserUse service not available"

**Symptoms:**
- Element detection fails
- Service connection errors

**Solutions:**
1. Check if service is running:
   ```bash
   curl http://localhost:4999/health
   ```
2. Start the service:
   ```bash
   python tools/browser_use_service.py
   ```
3. Check port 4999 is not in use
4. Review service logs for errors

## Test Execution Issues

### Tests fail with "Element not found"

**Common Causes:**
1. Website structure changed
2. Element requires scrolling
3. Popup or modal blocking element
4. Timing/loading issue

**Solutions:**
1. **Be more specific in your query:**
   - ❌ "search for products"
   - ✅ "search for shoes on Flipkart and get the first product name"

2. **Check the HTML logs:**
   - Open `robot_tests/{run-id}/log.html`
   - See which element failed
   - Check if locator is correct

3. **Regenerate the test:**
   - Websites change frequently
   - Simply run the query again

4. **Manual verification:**
   - Visit the website manually
   - Check if element exists
   - Verify no popups are blocking

### Tests timeout

**Symptoms:**
- Test runs for a long time
- Eventually fails with timeout

**Solutions:**
1. Increase timeout in `.env`:
   ```env
   BROWSER_USE_TIMEOUT=1200
   ```
2. Check website is accessible
3. Verify network connectivity
4. Simplify the test query

### Generated code has syntax errors

**Symptoms:**
- Test fails immediately
- Robot Framework syntax errors

**Solutions:**
1. Check the validation logs
2. Report the issue on GitHub with:
   - Your query
   - Generated code
   - Error message
3. Manually fix the `.robot` file if needed

### Docker container fails to start

**Symptoms:**
- Test execution fails
- Docker errors in logs

**Solutions:**
1. Check Docker has enough resources:
   - Memory: At least 4GB
   - Disk space: At least 10GB
2. Pull the latest images:
   ```bash
   docker pull python:3.9-slim
   ```
3. Clean up old containers:
   ```bash
   docker system prune -a
   ```

## AI Model Issues

### "Rate limit exceeded"

**Symptoms:**
- API calls fail
- Error about quota

**Solutions:**
1. **Using Gemini:**
   - Wait for rate limit to reset (per minute/day)
   - Upgrade to paid tier
   - Switch to Ollama for unlimited requests

2. **Check your usage:**
   - Visit [Google AI Studio](https://aistudio.google.com/)
   - Monitor your quota

### "Invalid API key"

**Symptoms:**
- Authentication errors
- API calls rejected

**Solutions:**
1. Verify API key is correct
2. Check key hasn't expired
3. Generate a new key at [Google AI Studio](https://aistudio.google.com/app/apikey)
4. Update `.env` file

### Ollama not working

**Symptoms:**
- Local model fails
- Connection errors

**Solutions:**
1. Verify Ollama is installed:
   ```bash
   ollama --version
   ```
2. Check Ollama is running:
   ```bash
   ollama list
   ```
3. Pull the required model:
   ```bash
   ollama pull llama3.1
   ```
4. Verify model name in `.env` matches available models

## Performance Issues

### Test generation is slow

**Possible Causes:**
- Slow network connection
- Complex query
- Model performance

**Solutions:**
1. Use faster models:
   ```env
   ONLINE_MODEL=gemini-2.5-flash
   ```
2. Simplify your query
3. Check network connectivity
4. Consider using local models (Ollama)

### High memory usage

**Symptoms:**
- System slows down
- Out of memory errors

**Solutions:**
1. Close unnecessary applications
2. Increase Docker memory limit
3. Clean up old test results:
   ```bash
   rm -rf robot_tests/old-run-ids/
   ```
4. Restart the services

## Debugging Tips

### Enable verbose logging

Add to `.env`:
```env
LOG_LEVEL=DEBUG
```

### Check application logs

Logs are in the `logs/` directory:
- `logs/crewai.log` - Agent workflow
- `logs/langchain.log` - LLM interactions
- `logs/litellm.log` - Model provider

### Logs appear twice

**Symptoms:**
- Each line is printed twice with two different formats
- Example: one line includes `root - INFO - ...`, another includes `[root] ...`

**Cause:**
- Multiple handlers are attached to the root logger
- This usually happens when an imported module calls `logging.basicConfig(...)` before app startup, and startup then adds handlers again

**Immediate fix:**
- Ensure root logging is configured once in `src/backend/main.py` with `force=True`

**Long-term prevention:**
1. Configure root logging only in application entrypoints (`src/backend/main.py`)
2. In library/tool modules, never call `logging.basicConfig(...)`
3. In modules, always use `logger = logging.getLogger(__name__)` and emit logs through that logger
4. If duplicate logs reappear, inspect root handlers at runtime:
   ```python
   import logging
   print(logging.getLogger().handlers)
   ```

### View test execution logs

Every test creates detailed logs:
```bash
# Open in browser
open robot_tests/{run-id}/log.html

# View in terminal
cat robot_tests/{run-id}/test.robot
```

### Test API directly

```bash
curl -X POST http://localhost:5000/generate-and-run \
  -H "Content-Type: application/json" \
  -d '{"query": "go to google.com"}'
```

### Run Robot Framework manually

```bash
robot --loglevel DEBUG robot_tests/{run-id}/test.robot
```

## Getting Help

If you're still stuck:

1. **Check existing issues**: [GitHub Issues](https://github.com/monkscode/Natural-Language-to-Robot-Framework/issues)
2. **Search discussions**: [GitHub Discussions](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions)
3. **Open a new issue** with:
   - Your query
   - Error message
   - Relevant log snippets
   - Environment details (OS, Python version, Docker version)

## Common Error Messages

### "ModuleNotFoundError: No module named 'X'"
**Solution**: Reinstall dependencies
```bash
pip install -r src/backend/requirements.txt
```

### "Permission denied"
**Solution**: Check file permissions or run with appropriate privileges
```bash
chmod +x run.sh test.sh
```

### "Connection refused"
**Solution**: Service not running or wrong port
- Check service is started
- Verify port in configuration
- Check firewall settings

### "Timeout waiting for element"
**Solution**: Element takes too long to load
- Increase timeout
- Check website is responsive
- Verify element exists on page
