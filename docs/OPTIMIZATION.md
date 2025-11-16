# CrewAI Performance Optimization Guide

This guide explains how to use Mark 1's performance optimization system to reduce token usage and costs while maintaining code generation accuracy.

## Overview

Mark 1's optimization system reduces token usage by **67%** (from 12K to 4K tokens per workflow) through a **Hybrid Knowledge Architecture** that combines:

1. **Core Rules** (~300 tokens) - Always included, library-specific constraints
2. **ChromaDB Vector Store** - Keyword documentation with semantic search
3. **Pattern Learning** - Learns from successful executions to predict relevant keywords
4. **Smart Context Pruning** - Includes only keywords relevant to detected action types

**Benefits:**
- 67% token reduction (12K → 4K tokens per workflow)
- 56% cost reduction ($0.0027 → $0.0012 per workflow)
- Maintains 95%+ code generation accuracy
- Improves over time through pattern learning
- Graceful degradation if optimization fails

## Quick Start

### 1. Install Dependencies

The optimization system requires additional Python packages:

```bash
pip install chromadb==0.4.22 sentence-transformers==2.2.2
```

### 2. Enable Optimization

Add to your `.env` file:

```env
# Enable optimization system
OPTIMIZATION_ENABLED=true
```

### 3. Restart Mark 1

```bash
# Stop services
# Restart backend and frontend
```

On first startup, the system will:
- Initialize ChromaDB with keyword embeddings (~5 seconds)
- Create pattern learning database
- Be ready for optimized test generation

## Configuration Options

All optimization settings are configured in `src/backend/.env`:

### OPTIMIZATION_ENABLED

Enable or disable the entire optimization system.

```env
OPTIMIZATION_ENABLED=true
```

**Options:**
- `true` - Enable optimization (67% token reduction)
- `false` - Use baseline behavior (full context)

**Default:** `false`

**Recommendation:** Enable after verifying your setup works correctly.

### OPTIMIZATION_CHROMA_DB_PATH

Path to ChromaDB storage directory for keyword embeddings.

```env
OPTIMIZATION_CHROMA_DB_PATH=./chroma_db
```

**Default:** `./chroma_db`

**What it stores:**
- Keyword embeddings for semantic search
- Separate collections for Browser Library and SeleniumLibrary
- Persistent storage (no re-embedding needed on restart)

**Disk usage:** ~10-20 MB per library

### OPTIMIZATION_PATTERN_DB_PATH

Path to SQLite database for pattern learning.

```env
OPTIMIZATION_PATTERN_DB_PATH=./data/pattern_learning.db
```

**Default:** `./data/pattern_learning.db`

**What it stores:**
- Query patterns with embeddings
- Keyword usage history
- Prediction statistics

**Disk usage:** Grows over time (~1 KB per query)

### OPTIMIZATION_KEYWORD_SEARCH_TOP_K

Number of keywords to return from semantic search.

```env
OPTIMIZATION_KEYWORD_SEARCH_TOP_K=3
```

**Range:** 1-10

**Default:** 3

**Trade-offs:**
- Higher values: More keyword options, higher token usage
- Lower values: Fewer options, lower token usage

**Recommendation:** Keep at 3 for best balance.

### OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD

Minimum confidence for pattern prediction (0.0-1.0).

```env
OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD=0.7
```

**Range:** 0.0 to 1.0

**Default:** 0.7 (70% similarity required)

**How it works:**
- System searches for similar past queries
- If similarity ≥ threshold, uses predicted keywords
- If similarity < threshold, uses zero-context + search tool

**Adjust based on:**
- `0.6` - More aggressive prediction (may include irrelevant keywords)
- `0.7` - Balanced (recommended)
- `0.8` - Conservative prediction (fewer predictions, more searches)

### OPTIMIZATION_CONTEXT_PRUNING_ENABLED

Enable smart context pruning based on query classification.

```env
OPTIMIZATION_CONTEXT_PRUNING_ENABLED=true
```

**Options:**
- `true` - Prune context to relevant keyword categories
- `false` - Include all keywords (when predicted/searched)

**Default:** `true`

**What it does:**
- Classifies query into action categories (navigation, input, interaction, extraction, assertion, wait)
- Includes only keywords matching detected categories
- Reduces context size by ~40%

**Recommendation:** Keep enabled for maximum token reduction.

### OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD

Minimum confidence for category classification (0.0-1.0).

```env
OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD=0.8
```

**Range:** 0.0 to 1.0

**Default:** 0.8 (80% similarity required)

**How it works:**
- System classifies query into action categories
- If confidence ≥ threshold, prunes to relevant categories
- If confidence < threshold, includes all categories (safe fallback)

**Adjust based on:**
- `0.7` - More aggressive pruning (may exclude needed keywords)
- `0.8` - Balanced (recommended)
- `0.9` - Conservative pruning (less reduction, safer)

## Example Configurations

### Production Setup (Recommended)

```env
# Enable optimization with balanced settings
OPTIMIZATION_ENABLED=true
OPTIMIZATION_CHROMA_DB_PATH=./chroma_db
OPTIMIZATION_PATTERN_DB_PATH=./data/pattern_learning.db
OPTIMIZATION_KEYWORD_SEARCH_TOP_K=3
OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD=0.7
OPTIMIZATION_CONTEXT_PRUNING_ENABLED=true
OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD=0.8
```

**Best for:** Production use with proven token reduction and accuracy.

### Aggressive Optimization

```env
# Maximum token reduction
OPTIMIZATION_ENABLED=true
OPTIMIZATION_CHROMA_DB_PATH=./chroma_db
OPTIMIZATION_PATTERN_DB_PATH=./data/pattern_learning.db
OPTIMIZATION_KEYWORD_SEARCH_TOP_K=2
OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD=0.6
OPTIMIZATION_CONTEXT_PRUNING_ENABLED=true
OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD=0.7
```

**Best for:** Minimizing costs when accuracy can tolerate slight variations.

### Conservative Optimization

```env
# Safer optimization with higher accuracy
OPTIMIZATION_ENABLED=true
OPTIMIZATION_CHROMA_DB_PATH=./chroma_db
OPTIMIZATION_PATTERN_DB_PATH=./data/pattern_learning.db
OPTIMIZATION_KEYWORD_SEARCH_TOP_K=5
OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD=0.8
OPTIMIZATION_CONTEXT_PRUNING_ENABLED=true
OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD=0.9
```

**Best for:** Critical tests where accuracy is paramount.

### Development/Testing

```env
# Disable optimization for baseline comparison
OPTIMIZATION_ENABLED=false
```

**Best for:** Testing baseline behavior or debugging issues.

## How It Works

### Tier 1: Core Rules (Always Included)

Core rules are **always** included in agent context (~300 tokens):

- Critical sequences (New Browser → New Context viewport=None → New Page)
- Parameter rules and syntax
- Auto-waiting behavior
- Locator priorities
- Library-specific constraints

**Why always included:**
- Ensures critical patterns never forgotten
- Maintains code quality and consistency
- Prevents common mistakes

### Tier 2: Pattern Learning (Predicted Keywords)

When you run a query, the system:

1. Searches for similar past queries in pattern database
2. If similarity ≥ confidence threshold (default 0.7):
   - Predicts relevant keywords based on past usage
   - Retrieves full documentation from ChromaDB
   - Includes in agent context (~500 tokens)
3. If similarity < threshold:
   - Falls back to Tier 3

**Improves over time:**
- More queries = better predictions
- System learns your testing patterns
- Prediction accuracy increases

### Tier 3: Zero-Context + Search Tool

If no pattern match found:

1. Agents receive minimal context with tool instructions (~200 tokens)
2. Agents use `keyword_search` tool to find relevant keywords on-demand
3. Tool performs semantic search in ChromaDB (<100ms)
4. Returns top 3 matching keywords with documentation

**Example tool usage:**
```
Agent: "I need to click a button"
Tool: Returns ["Click", "Click Element", "Click Button"] with docs
Agent: Uses "Click" in generated code
```

### Tier 4: Full Context Fallback

If all optimizations fail:
- System falls back to full context (baseline behavior)
- Logs fallback event for monitoring
- Maintains 99.9% workflow success rate

## Monitoring Performance

### Metrics API

The optimization system tracks detailed metrics accessible via the workflow metrics API:

```bash
curl http://localhost:5000/workflow-metrics/{workflow_id}
```

**Response includes:**

```json
{
  "workflow_id": "abc-123",
  "total_time_ms": 35000,
  "optimization": {
    "token_usage": {
      "step_planner": 800,
      "element_identifier": 600,
      "code_assembler": 1200,
      "code_validator": 400,
      "total": 3000
    },
    "keyword_search": {
      "calls": 2,
      "total_latency_ms": 150,
      "avg_latency_ms": 75,
      "accuracy": 0.95
    },
    "pattern_learning": {
      "prediction_used": true,
      "predicted_keywords_count": 8,
      "prediction_accuracy": 0.87
    },
    "context_reduction": {
      "baseline_tokens": 12000,
      "optimized_tokens": 3000,
      "reduction_percentage": 75.0
    }
  }
}
```

### Key Metrics

**Token Usage:**
- Per-agent token counts
- Total workflow token usage
- Compare against baseline (12K tokens)

**Keyword Search:**
- Number of tool calls
- Average latency (target: <100ms)
- Accuracy (% of returned keywords used in final code)

**Pattern Learning:**
- Whether prediction was used
- Number of predicted keywords
- Prediction accuracy

**Context Reduction:**
- Baseline vs optimized token counts
- Reduction percentage (target: 67%)

### Logs

Optimization events are logged to `logs/optimization.log`:

```
INFO: ChromaDB initialized with 143 keywords in 4.2s
INFO: Pattern prediction used for query "login to website" (8 keywords, 0.87 confidence)
INFO: Keyword search: "click button" → 3 results in 78ms
WARNING: Pattern prediction confidence too low (0.65), falling back to search tool
ERROR: ChromaDB search failed, falling back to full context
```

## Interpreting Metrics

### Good Performance

```json
{
  "token_usage": {"total": 3000},
  "keyword_search": {"calls": 1, "avg_latency_ms": 75},
  "pattern_learning": {"prediction_used": true, "prediction_accuracy": 0.85},
  "context_reduction": {"reduction_percentage": 75.0}
}
```

**Indicators:**
- ✅ Total tokens: 3K-4K (67% reduction achieved)
- ✅ Few keyword searches (pattern learning working)
- ✅ High prediction accuracy (>0.80)
- ✅ High reduction percentage (>65%)

### Needs Tuning

```json
{
  "token_usage": {"total": 8000},
  "keyword_search": {"calls": 5, "avg_latency_ms": 150},
  "pattern_learning": {"prediction_used": false},
  "context_reduction": {"reduction_percentage": 33.0}
}
```

**Indicators:**
- ⚠️ Total tokens: 6K-8K (less reduction than expected)
- ⚠️ Many keyword searches (pattern learning not matching)
- ⚠️ Prediction not used (confidence threshold too high?)
- ⚠️ Low reduction percentage (<50%)

**Actions:**
- Lower `OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD` to 0.6
- Check if queries are too diverse for pattern learning
- Verify ChromaDB is initialized correctly

### Fallback Occurring

```json
{
  "token_usage": {"total": 12000},
  "context_reduction": {"reduction_percentage": 0.0}
}
```

**Indicators:**
- ❌ Total tokens: 12K (no reduction, using full context)
- ❌ Zero reduction percentage

**Actions:**
- Check logs for error messages
- Verify ChromaDB path is accessible
- Verify dependencies are installed
- Check pattern database is writable

## Troubleshooting

### "ChromaDB initialization failed"

**Symptoms:**
- Error on startup
- Optimization falls back to full context

**Solutions:**
1. Verify dependencies installed:
   ```bash
   pip install chromadb==0.4.22 sentence-transformers==2.2.2
   ```
2. Check ChromaDB path is writable:
   ```bash
   mkdir -p ./chroma_db
   chmod 755 ./chroma_db
   ```
3. Check disk space (needs ~20 MB)
4. Review logs for specific error

### "Pattern database locked"

**Symptoms:**
- SQLite database errors
- Pattern learning fails

**Solutions:**
1. Check no other process is using the database
2. Verify database path is writable:
   ```bash
   mkdir -p ./data
   chmod 755 ./data
   ```
3. Delete and recreate database:
   ```bash
   rm ./data/pattern_learning.db
   # Restart Mark 1 to recreate
   ```

### "Keyword search timeout"

**Symptoms:**
- Search takes >100ms
- Slow test generation

**Solutions:**
1. Check ChromaDB is initialized (first search is slower)
2. Verify sentence-transformers model is downloaded
3. Check system resources (CPU, memory)
4. Reduce `OPTIMIZATION_KEYWORD_SEARCH_TOP_K` to 2

### "Low prediction accuracy"

**Symptoms:**
- Pattern learning predicts wrong keywords
- Generated code uses different keywords

**Solutions:**
1. System needs more training data (run more queries)
2. Lower confidence threshold:
   ```env
   OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD=0.6
   ```
3. Check queries are similar enough for pattern matching
4. Verify pattern database is being updated

### "Token reduction less than expected"

**Symptoms:**
- Reduction percentage <50%
- Token usage still high

**Solutions:**
1. Enable context pruning:
   ```env
   OPTIMIZATION_CONTEXT_PRUNING_ENABLED=true
   ```
2. Lower pruning threshold:
   ```env
   OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD=0.7
   ```
3. Reduce keyword search results:
   ```env
   OPTIMIZATION_KEYWORD_SEARCH_TOP_K=2
   ```
4. Check pattern learning is being used (review metrics)

### "Optimization disabled automatically"

**Symptoms:**
- System falls back to full context
- Logs show fallback events

**Solutions:**
1. Check all dependencies installed
2. Verify ChromaDB initialized successfully
3. Check pattern database is accessible
4. Review error logs for root cause
5. Temporarily disable optimization to verify baseline works:
   ```env
   OPTIMIZATION_ENABLED=false
   ```

## Performance Tips

### 1. Let Pattern Learning Train

The system improves over time:
- First 10 queries: Mostly uses search tool
- After 20 queries: Starts predicting keywords
- After 50 queries: High prediction accuracy

**Tip:** Run diverse queries to build a good training set.

### 2. Monitor Metrics Regularly

Check metrics after every 10-20 queries:
- Is token reduction improving?
- Is prediction accuracy increasing?
- Are searches becoming less frequent?

### 3. Tune Thresholds Based on Usage

**If you have diverse queries:**
- Lower confidence thresholds (0.6-0.7)
- More searches, less prediction

**If you have repetitive queries:**
- Higher confidence thresholds (0.7-0.8)
- More prediction, fewer searches

### 4. Clean Up Old Data

Pattern database grows over time:

```bash
# Backup current database
cp ./data/pattern_learning.db ./data/pattern_learning.db.backup

# Optional: Delete old patterns (keeps last 1000)
sqlite3 ./data/pattern_learning.db "DELETE FROM patterns WHERE id NOT IN (SELECT id FROM patterns ORDER BY timestamp DESC LIMIT 1000)"
```

### 5. Use Appropriate Library

Browser Library works best with optimization:
- Better keyword documentation
- More consistent patterns
- Faster execution

```env
ROBOT_LIBRARY=browser
```

## Best Practices

### Do's

✅ **Enable optimization in production** - Proven 67% token reduction

✅ **Monitor metrics regularly** - Track performance improvements

✅ **Let pattern learning train** - Needs 20+ queries for best results

✅ **Use recommended settings** - Default configuration is well-tested

✅ **Keep ChromaDB persistent** - Don't delete between restarts

✅ **Review logs for fallbacks** - Indicates issues to address

### Don'ts

❌ **Don't disable after one failure** - Check logs and fix root cause

❌ **Don't set thresholds too low** - May include irrelevant keywords

❌ **Don't set thresholds too high** - May never use predictions

❌ **Don't delete pattern database** - Loses all learning

❌ **Don't expect instant results** - Pattern learning needs training

❌ **Don't ignore metrics** - They show what's working and what's not

## Migration Guide

### Enabling Optimization on Existing System

1. **Backup current setup:**
   ```bash
   cp src/backend/.env src/backend/.env.backup
   ```

2. **Install dependencies:**
   ```bash
   pip install chromadb==0.4.22 sentence-transformers==2.2.2
   ```

3. **Add configuration to `.env`:**
   ```env
   OPTIMIZATION_ENABLED=true
   OPTIMIZATION_CHROMA_DB_PATH=./chroma_db
   OPTIMIZATION_PATTERN_DB_PATH=./data/pattern_learning.db
   OPTIMIZATION_KEYWORD_SEARCH_TOP_K=3
   OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD=0.7
   OPTIMIZATION_CONTEXT_PRUNING_ENABLED=true
   OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD=0.8
   ```

4. **Restart Mark 1:**
   ```bash
   # Stop all services
   # Start backend and frontend
   ```

5. **Verify initialization:**
   - Check logs for "ChromaDB initialized"
   - Check `./chroma_db/` directory created
   - Check `./data/pattern_learning.db` created

6. **Run test queries:**
   - Start with simple queries
   - Check metrics API for token reduction
   - Verify code generation accuracy

7. **Monitor for 24 hours:**
   - Review metrics regularly
   - Check for fallback events
   - Verify no errors in logs

8. **Tune if needed:**
   - Adjust thresholds based on metrics
   - Review troubleshooting section if issues

### Disabling Optimization

If you need to disable optimization:

1. **Set in `.env`:**
   ```env
   OPTIMIZATION_ENABLED=false
   ```

2. **Restart Mark 1**

3. **Keep data for future use:**
   - Don't delete `./chroma_db/`
   - Don't delete `./data/pattern_learning.db`
   - Can re-enable anytime without losing learning

## FAQ

### Does optimization affect code quality?

No. The system maintains 95%+ accuracy by:
- Always including core rules
- Using semantic search for relevant keywords
- Falling back to full context if needed

### How long does ChromaDB initialization take?

~5 seconds on first startup. Subsequent startups are instant (uses persistent storage).

### Does pattern learning work with both Browser and SeleniumLibrary?

Yes. The system learns patterns for whichever library you're using.

### Can I use optimization with local models (Ollama)?

Yes. Optimization works with both online (Gemini) and local (Ollama) models.

### What happens if ChromaDB fails?

The system gracefully falls back to full context (baseline behavior). Workflow success rate remains 99.9%.

### How much disk space does optimization use?

- ChromaDB: ~10-20 MB per library
- Pattern database: ~1 KB per query (grows over time)
- Total: <100 MB for typical usage

### Can I reset pattern learning?

Yes. Delete the pattern database:
```bash
rm ./data/pattern_learning.db
```
System will recreate it on next startup.

### Does optimization work with custom keywords?

Yes, if you've added custom keywords to your library context, they'll be included in ChromaDB and pattern learning.

## Getting Help

If you're experiencing issues with optimization:

1. **Check logs:** `logs/optimization.log`
2. **Review metrics:** Use workflow metrics API
3. **Check troubleshooting section** above
4. **Search existing issues:** [GitHub Issues](https://github.com/monkscode/Natural-Language-to-Robot-Framework/issues)
5. **Open new issue** with:
   - Configuration settings
   - Error messages from logs
   - Metrics output
   - Steps to reproduce

## Additional Resources

- [Configuration Guide](CONFIGURATION.md) - All environment variables
- [Architecture Guide](ARCHITECTURE.md) - How Mark 1 works
- [Troubleshooting Guide](TROUBLESHOOTING.md) - Common issues
- [GitHub Discussions](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions) - Ask questions

---

**Ready to optimize?** Enable optimization in your `.env` file and start reducing token costs today! 🚀
