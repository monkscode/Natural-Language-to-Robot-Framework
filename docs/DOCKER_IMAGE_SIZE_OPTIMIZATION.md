# Docker Image Size Optimization Guide

## Current Image Sizes (Baseline)

| Image | Current Size | Components |
|-------|-------------|------------|
| **FastAPI** | 4.33 GB | Python, FastAPI, ChromaDB, PyTorch CPU, Chrome, Selenium, Robot Framework |
| **Browser Service** | 2.66 GB | Python, Flask, Playwright, Chrome, Browser automation tools |

## Size Analysis Breakdown

### FastAPI Image (4.33 GB)

**Major Contributors:**
1. **PyTorch CPU (~1.5 GB)** - Required by sentence-transformers for ChromaDB embeddings
2. **Google Chrome Stable (~200-300 MB)** - Required by SeleniumScrapingTool (currently unused)
3. **ChromaDB + Dependencies (~500 MB)** - Vector database with numpy, sentence-transformers
4. **Python Base Image (~140 MB)** - python:3.12-slim
5. **Robot Framework + Libraries (~200 MB)** - robotframework-browser, playwright, selenium
6. **Other Dependencies (~1+ GB)** - crewai, langchain, litellm, various tools

### Browser Service Image (2.66 GB)

**Major Contributors:**
1. **Google Chrome Stable (~200-300 MB)** - Required for browser automation
2. **Playwright Chromium (~400 MB)** - Additional browser for Playwright
3. **Python Base Image (~140 MB)** - python:3.12-slim
4. **Browser dependencies (~1.5+ GB)** - System libraries, browser-use, playwright dependencies

---

## Optimization Strategies

### 🎯 Quick Wins (Easy Implementation)

#### 1. **Remove Unused Chrome from FastAPI** (Save ~200-300 MB)
**Impact:** Medium | **Effort:** Low | **Risk:** Low

The SeleniumScrapingTool is initialized but never used (only `batch_browser_use_tool` is used). Remove Chrome installation from `Dockerfile.fastapi`.

**Implementation:**
```dockerfile
# In Dockerfile.fastapi
# Remove Chrome installation lines:
# - wget chrome gpg key
# - add chrome repo
# - apt-get install google-chrome-stable
```

**Code Changes Required:**
- Remove Chrome/Selenium initialization from `src/backend/crew_ai/agents.py`
- Confirm no other code paths use SeleniumScrapingTool

**Estimated Size:** 4.33 GB → **4.0 GB** (-300 MB)

---

#### 2. **Use Multi-Stage Builds** (Save ~500 MB per image)
**Impact:** High | **Effort:** Medium | **Risk:** Low

Build dependencies in one stage, copy only runtime artifacts to final stage.

**Implementation:**
```dockerfile
# Stage 1: Builder
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install uv
COPY requirements.txt .
RUN uv pip install --prefix=/install --no-cache -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim
COPY --from=builder /install /usr/local
COPY src/ /app/src/
...
```

**Estimated Size per image:** -500 MB (removes build cache, intermediate files)

---

#### 3. **Optimize Python Dependencies** (Save ~200-400 MB)
**Impact:** Medium | **Effort:** Medium | **Risk:** Medium

**Options:**

a) **Use `--no-deps` for selective packages:**
```bash
# Install only necessary sub-dependencies
uv pip install torch --no-deps --index-url https://download.pytorch.org/whl/cpu
```

b) **Split ChromaDB to separate service:**
- Move ChromaDB to a dedicated microservice
- FastAPI talks to ChromaDB via HTTP API
- Only users needing optimization feature run ChromaDB container

c) **Use lighter alternatives:**
- Replace `sentence-transformers` with `transformers` + manual model loading
- Use smaller embedding models (e.g., `all-MiniLM-L6-v2` instead of default)

**Estimated Size:** -200-400 MB depending on approach

---

### 🚀 Advanced Optimizations (Moderate Effort)

#### 4. **Use Alpine Linux Base** (Save ~100 MB per image)
**Impact:** Medium | **Effort:** High | **Risk:** High

Replace `python:3.12-slim` (Debian-based) with `python:3.12-alpine` (Alpine Linux).

**Challenges:**
- Chrome/Playwright may not work well on Alpine
- Need to compile some Python packages from source
- System dependencies have different names

**Recommendation:** Only attempt if comfortable with Alpine Linux ecosystem.

**Estimated Size:** -100 MB per image

---

#### 5. **Share Chrome Installation via Bind Mount** (Save ~200 MB)
**Impact:** Medium | **Effort:** Medium | **Risk:** Medium

Install Chrome once on the host, mount into containers.

**Implementation:**
```yaml
# docker-compose.yml
volumes:
  - /usr/bin/google-chrome:/usr/bin/google-chrome:ro
  - /opt/google/chrome:/opt/google/chrome:ro
```

**Caveats:**
- Host must have Chrome installed
- Less portable across environments
- Breaks container isolation philosophy

---

#### 6. **Use Distroless Images** (Save ~50-100 MB)
**Impact:** Low | **Effort:** High | **Risk:** Medium

Use Google's distroless Python images (no shell, minimal OS).

**Example:**
```dockerfile
FROM gcr.io/distroless/python3-debian12
```

**Challenges:**
- No shell for debugging
- Limited system utilities
- Chrome may not work

---

### 🔬 Experimental Optimizations

#### 7. **Layer Caching Optimization**
**Impact:** Build Speed | **Effort:** Low | **Risk:** None

Order Dockerfile commands to maximize cache hits:

```dockerfile
# 1. Install system packages (rarely change)
RUN apt-get install ...

# 2. Install Python dependencies (change occasionally)
COPY requirements.txt .
RUN pip install -r requirements.txt

# 3. Copy application code (changes frequently)
COPY src/ /app/src/
```

---

#### 8. **Compress Image Layers**
**Impact:** Medium | **Effort:** Low | **Risk:** None

Use Docker's `--squash` flag or BuildKit compression.

```bash
docker buildx build --compression-level=9 --compression=zstd ...
```

**Note:** May increase build time but reduces layer size.

---

## Recommended Implementation Plan

### Phase 1: Quick Wins (Target: 3.5 GB total)
1. ✅ Remove Chrome from FastAPI image (-300 MB)
2. ✅ Implement multi-stage builds for both images (-1 GB total)
3. ✅ Optimize Python dependency installation

**Expected Result:**
- FastAPI: 4.33 GB → **3.0 GB**
- Browser Service: 2.66 GB → **2.0 GB**

### Phase 2: Advanced (Target: 2.5 GB total)
1. Split ChromaDB into separate optional service (-500 MB from FastAPI)
2. Share Chrome installation via volume mount (-200 MB from Browser Service)

**Expected Result:**
- FastAPI: 3.0 GB → **2.0 GB**
- Browser Service: 2.0 GB → **1.8 GB**
- ChromaDB (optional): **500 MB**

### Phase 3: Experimental (Target: < 2 GB total)
1. Evaluate Alpine Linux feasibility
2. Test distroless images for production

---

## Trade-offs to Consider

| Optimization | Size Savings | Build Time | Runtime Performance | Maintainability |
|-------------|--------------|------------|-------------------|----------------|
| Remove unused Chrome | 300 MB | Same | Same | ✅ Better |
| Multi-stage builds | 500 MB | +20% | Same | ✅ Better |
| Alpine Linux | 100 MB | +50% | -5% | ⚠️ Harder |
| Split ChromaDB | 500 MB | Same | -2% (network) | ⚠️ More complex |
| Distroless | 100 MB | Same | Same | ❌ Harder debugging |

---

## Monitoring Image Size

### Check Current Sizes
```bash
docker images | grep nlrf
```

### Analyze Layers
```bash
docker history monkscode/nlrf-fastapi:latest
```

### Use Dive Tool (Detailed Analysis)
```bash
docker run --rm -it \
  -v /var/run/docker.sock:/var/run/docker.sock \
  wagoodman/dive monkscode/nlrf-fastapi:latest
```

---

## Conclusion

**Realistic Target Sizes:**
- FastAPI: **2.5-3.0 GB** (from 4.33 GB) - 30-40% reduction
- Browser Service: **1.8-2.0 GB** (from 2.66 GB) - 25-30% reduction

**Total Savings:** ~1.5-2 GB (30-35% overall reduction)

**Recommended Next Steps:**
1. Remove Chrome from FastAPI (easy, safe, 300 MB)
2. Implement multi-stage builds (medium effort, 1 GB total)
3. Evaluate ChromaDB separation for users who need it

The current sizes are reasonable for a full-featured AI-powered test automation platform. Focus optimization efforts based on:
- **Deployment constraints** (bandwidth, storage)
- **Build/push frequency** (CI/CD speed)
- **User requirements** (optional features)
