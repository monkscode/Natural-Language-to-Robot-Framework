# LLM Traces Guide

How to view, query, and interpret LLM traces captured by the Mark 1 observability system.

---

## What are traces?

Every time the system calls a language model (Gemini, Vertex AI, Ollama), it records a **trace row** containing:

- Which model was called
- How many tokens were used (prompt + completion)
- What it cost in USD
- How long the call took (latency in ms)
- Which workflow triggered it
- The full prompt text and response text

Traces are stored in a local SQLite database at `data/llm_traces.db`. They persist between restarts and accumulate over time.

---

## Prerequisites

Tracing is controlled by one environment variable in `src/backend/.env`:

```
OBSERVABILITY_BACKEND=sqlite
```

| Value | Behaviour |
|---|---|
| `sqlite` | Traces written to `data/llm_traces.db` (default, zero setup) |
| `none` | Tracing disabled entirely |
| `grafana` | Exported to Grafana Tempo via OTLP (requires docker-compose.grafana.yml) |
| `otlp` | Exported to any OTLP endpoint (Langfuse, Jaeger, Datadog) |

**For local development `sqlite` is all you need** — no extra infrastructure required.

Confirm tracing is active by checking the startup log:

```
[OBSERVABILITY] OpenLLMetry initialized — backend=sqlite, prompts=captured
[LLM_TRACE] LiteLLM trace callback registered
```

---

## Viewing traces via the API

The FastAPI service (port 5000) exposes four read-only trace endpoints. No authentication is required.

All trace endpoints are under the `/api/admin/traces` path prefix.

### 1. List recent traces

```
GET http://localhost:5000/api/admin/traces/
```

Returns the 50 most recent LLM call records (metadata only, no prompt/response text).

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `llm_only` | bool | `true` | Show only `*.litellm` spans — the per-call records with real token/cost data. Set `false` to also see OTel agent spans, HTTP, ChromaDB, and other infrastructure spans |
| `workflow_id` | string | — | Filter to one workflow run |
| `model` | string | — | Partial match on model name (e.g. `gemini`) |
| `status` | string | — | `OK` or `ERROR` |
| `limit` | int | `50` | Max rows to return (1–500) |
| `offset` | int | `0` | Skip N rows for pagination |

> **Important:** The database stores spans for all instrumented operations — LLM calls, ChromaDB writes, HTTP requests, etc. By default (`llm_only=true`) only LLM call spans are returned. Infrastructure spans have `model: null` and `tokens: 0` by design — they are not broken LLM traces.

**Example — list last 10 LLM calls:**

```bash
curl "http://localhost:5000/api/admin/traces/?limit=10"
```

**Example — include all span types:**

```bash
curl "http://localhost:5000/api/admin/traces/?llm_only=false&limit=20"
```

**Sample response:**

```json
{
  "traces": [
    {
      "id": "1a2b3c4d5e6f7890",
      "trace_id": "abcdef1234567890abcdef1234567890",
      "name": "gemini-2.5-flash.litellm",
      "duration_ms": 4823.5,
      "status": "OK",
      "model": "gemini-2.5-flash",
      "prompt_tokens": 11840,
      "completion_tokens": 395,
      "cost_usd": 0.004539,
      "workflow_id": "d8083b13-61cf-45ff-9a6d-13ffbf07b13d",
      "created_at": "2026-04-18 11:38:58"
    }
  ],
  "limit": 10,
  "offset": 0
}
```

---

### 2. Full detail for a single span (includes prompt + response)

```
GET http://localhost:5000/api/admin/traces/{span_id}
```

The `span_id` is the `id` field from the list response.

```bash
curl "http://localhost:5000/api/admin/traces/1a2b3c4d5e6f7890"
```

**Sample response (truncated):**

```json
{
  "id": "1a2b3c4d5e6f7890",
  "name": "gemini-2.5-flash.litellm",
  "model": "gemini-2.5-flash",
  "prompt_tokens": 11840,
  "completion_tokens": 395,
  "total_tokens": 12235,
  "cost_usd": 0.004539,
  "duration_ms": 4823.5,
  "status": "OK",
  "workflow_id": "d8083b13-61cf-45ff-9a6d-13ffbf07b13d",
  "prompt_text": "You are a Test Automation Planner...",
  "response_text": "Step 1: Navigate to...",
  "created_at": "2026-04-18 11:38:58"
}
```

> The `prompt_text` and `response_text` fields let you see exactly what the agent sent and received — useful for debugging wrong outputs.

---

### 3. All LLM calls for one workflow (primary debugging view)

```
GET http://localhost:5000/api/admin/traces/workflow/{workflow_id}
```

Shows every LLM call made during a single test generation run, in chronological order, with a cost and token summary.

**Finding the workflow ID:** it appears in the SSE stream in the browser UI, in `logs/application.log`, or in the list traces response.

```bash
curl "http://localhost:5000/api/admin/traces/workflow/d8083b13-61cf-45ff-9a6d-13ffbf07b13d"
```

**Sample response:**

```json
{
  "workflow_id": "d8083b13-61cf-45ff-9a6d-13ffbf07b13d",
  "llm_calls": 8,
  "total_cost_usd": 0.033621,
  "total_tokens": 72839,
  "total_duration_ms": 38241.5,
  "traces": [
    {
      "name": "gemini-2.5-flash.litellm",
      "model": "gemini-2.5-flash",
      "prompt_tokens": 3432,
      "completion_tokens": 538,
      "cost_usd": 0.002374,
      "duration_ms": 3102.1,
      ...
    }
  ]
}
```

This tells you:
- How many LLM calls the 4-agent pipeline made in total
- The full cost of a single test generation run
- Which agent was expensive (large prompt_tokens = wide context)
- Which call was slow (high duration_ms)

---

### 4. Cost summary across all runs

```
GET http://localhost:5000/api/admin/traces/stats/cost
```

Aggregates token usage and cost by model for the last N days.

```bash
# Last 7 days (default)
curl "http://localhost:5000/api/admin/traces/stats/cost"

# Last 30 days
curl "http://localhost:5000/api/admin/traces/stats/cost?last_days=30"
```

**Sample response:**

```json
{
  "period_days": 7,
  "total_llm_calls": 64,
  "total_cost_usd": 0.271834,
  "total_prompt_tokens": 489231,
  "total_completion_tokens": 31824,
  "avg_latency_ms": 4201.3,
  "total_workflows": 8,
  "per_model": [
    {
      "model": "gemini-2.5-flash",
      "calls": 64,
      "cost": 0.271834,
      "tokens": 521055,
      "avg_latency_ms": 4201.3
    }
  ]
}
```

---

## Querying the database directly (SQLite)

The database is at `data/llm_traces.db`. You can query it with any SQLite client or the Python sqlite3 CLI.

### Open with Python

```bash
# From project root with venv activated
python -c "
import sqlite3
conn = sqlite3.connect('data/llm_traces.db')
conn.row_factory = sqlite3.Row
rows = conn.execute('''
    SELECT name, model, prompt_tokens, completion_tokens, cost_usd, workflow_id, created_at
    FROM llm_traces
    WHERE model IS NOT NULL
    ORDER BY created_at DESC
    LIMIT 20
''').fetchall()
for r in rows:
    print(dict(r))
conn.close()
"
```

### Open with the sqlite3 CLI

```bash
sqlite3 data/llm_traces.db
```

```sql
-- Show all LLM call traces
SELECT name, model, prompt_tokens, completion_tokens, cost_usd, workflow_id
FROM llm_traces
WHERE model IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;

-- Cost breakdown by model (all time)
SELECT model, COUNT(*) as calls, ROUND(SUM(cost_usd), 4) as total_cost_usd, SUM(total_tokens) as tokens
FROM llm_traces
WHERE model IS NOT NULL
GROUP BY model;

-- All calls for one workflow
SELECT name, prompt_tokens, completion_tokens, cost_usd, duration_ms
FROM llm_traces
WHERE workflow_id = 'd8083b13-61cf-45ff-9a6d-13ffbf07b13d'
ORDER BY start_time_ns ASC;

-- Most expensive workflows
SELECT workflow_id, COUNT(*) as calls, ROUND(SUM(cost_usd), 5) as cost
FROM llm_traces
WHERE workflow_id IS NOT NULL
GROUP BY workflow_id
ORDER BY cost DESC
LIMIT 10;
```

### Recommended GUI tools

| Tool | Platform | Notes |
|---|---|---|
| [DB Browser for SQLite](https://sqlitebrowser.org/) | Windows/Mac/Linux | Free, visual table browser |
| [TablePlus](https://tableplus.com/) | Mac/Windows | Paid, clean UI |
| DBeaver | All | Free, open source |

Open `data/llm_traces.db` in any of these to browse and filter traces visually without writing SQL.

---

## Understanding the trace schema

| Column | Type | Description |
|---|---|---|
| `id` | TEXT | Unique span ID (16-char hex) |
| `trace_id` | TEXT | Groups all spans from one workflow together |
| `parent_span_id` | TEXT | Parent span ID for nested calls (nullable) |
| `name` | TEXT | Span name — see types below |
| `start_time_ns` / `end_time_ns` | INTEGER | Unix nanoseconds |
| `duration_ms` | REAL | Call latency in milliseconds |
| `status` | TEXT | `OK` or `ERROR` |
| `model` | TEXT | LiteLLM model string (e.g. `gemini-2.5-flash`) |
| `prompt_text` | TEXT | Full prompt sent to the model (may be NULL for OTel spans) |
| `response_text` | TEXT | Full response from the model (may be NULL for OTel spans) |
| `prompt_tokens` | INTEGER | Input token count |
| `completion_tokens` | INTEGER | Output token count |
| `total_tokens` | INTEGER | prompt + completion |
| `cost_usd` | REAL | USD cost calculated by LiteLLM's pricing tables |
| `workflow_id` | TEXT | UUID linking the trace to a specific test generation run |
| `attributes_json` | TEXT | Raw OTel span attributes as JSON |
| `created_at` | TEXT | SQLite datetime string |

### Span name types

| Name pattern | Source | Has tokens + cost? |
|---|---|---|
| `gemini-2.5-flash.litellm` | LiteLLM success callback (per-call) | Yes — most useful |
| `crewai.workflow` | CrewAI OTel instrumentation (aggregate) | Tokens yes, cost no |
| `Test Automation Planner.agent` | CrewAI OTel (per-agent) | No (instrumentation bug) |
| `chroma.add`, `GET`, `POST` | OTel auto-instrumentation for HTTP/DB | No |
| `test-generation-workflow` | Custom workflow root span | No |

**The `*.litellm` rows are the most useful** — they have per-call token counts, cost, and are linked to the workflow via `workflow_id`.

---

## Common debugging scenarios

### "Why did my test generation produce wrong output?"

1. Find the workflow ID from the UI or logs.
2. Call `GET /api/admin/traces/workflow/{workflow_id}` to get all LLM calls in order.
3. Find the call where things went wrong — look for a high `completion_tokens` (verbose/hallucinated response) or `status: ERROR`.
4. Call `GET /api/admin/traces/{span_id}` on that span to see the exact prompt and response.

### "How much did that run cost?"

```bash
curl "http://localhost:5000/api/admin/traces/workflow/{workflow_id}"
# → look at total_cost_usd in the summary
```

### "Which agent is consuming the most tokens?"

Get the workflow trace and sort by `prompt_tokens` descending. The agent with the largest prompt is consuming the most context — usually the Code Assembler (Agent 2) since it receives all prior agent outputs.

### "Why is the cost showing $0.00?"

Check that `OBSERVABILITY_BACKEND=sqlite` is set in `src/backend/.env` and that the service was restarted after the change. If `OBSERVABILITY_BACKEND=none`, no data is written.

Also confirm the LiteLLM callback registered on startup:

```bash
grep "LiteLLM trace callback registered" logs/application.log
```

### "The database doesn't exist yet"

`data/llm_traces.db` is created automatically on first run. The API returns an empty list (not an error) until the first trace is recorded.

---

## Data retention

Traces accumulate indefinitely. To clean up old traces (older than 30 days):

```python
from src.backend.core.trace_store import get_trace_store
store = get_trace_store()
deleted = store.cleanup_old_traces(max_age_days=30)
print(f"Deleted {deleted} old traces")
```

To delete everything and start fresh:

```bash
rm data/llm_traces.db
```

The database will be recreated automatically on the next run.
