# Docker Containerization & Local Deployment

## Overview

The OptiBot pipeline is containerized with Docker to ensure **reproducible execution** across local development, CI/CD, and production environments.

---

## Dockerfile Structure

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV STATE_FILE=/app/state/state.json

CMD ["python", "main.py"]
```

### Key Decisions

| Line | Purpose |
|---|---|
| `python:3.11-slim` | Minimal base image (~150 MB vs. 900 MB for `python:3.11`) |
| `WORKDIR /app` | All subsequent commands run inside container's `/app` |
| `--no-cache-dir` | Reduces image size by skipping pip cache |
| `ENV STATE_FILE=...` | Sets default path; can be overridden at runtime |
| `CMD ["python", "main.py"]` | Runs pipeline when container starts |

---

## Local Testing

### Build Image

```bash
docker build -t optibot .
```

### Run with Environment Variables

**PowerShell:**
```powershell
docker run --rm `
  -e GEMINI_API_KEY="your-api-key" `
  -e GEMINI_STORE_NAME="fileSearchStores/optisignssupportdocs-xxx" `
  -e STATE_FILE=/app/state/state.json `
  -v "${PWD}:/app/state" `
  optibot
```

**Bash:**
```bash
docker run --rm \
  -e GEMINI_API_KEY="your-api-key" \
  -e GEMINI_STORE_NAME="fileSearchStores/optisignssupportdocs-xxx" \
  -e STATE_FILE=/app/state/state.json \
  -v "$(pwd):/app/state" \
  optibot
```

### Flags Explained

| Flag | Purpose |
|---|---|
| `--rm` | Delete container after exit (cleanup) |
| `-e KEY=value` | Pass environment variables into container |
| `-v $(pwd):/app/state` | Mount current directory to `/app/state` inside container |

### Why Volume Mount?

Without `-v`, container's filesystem is isolated:
- `state.json` written inside container → lost when container exits
- With `-v`, the mounted directory maps to host filesystem → `state.json` persists

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success (pipeline completed; at least one article processed or all skipped) |
| `1` | Failure (critical error: API connection failed, invalid config, etc.) |

GitHub Actions checks exit code to determine workflow success/failure.

---

## Container vs. Virtual Environment

| Aspect | Docker Container | Python venv |
|---|---|---|
| **Isolation** | Full OS-level isolation | Only Python packages isolated |
| **Reproducibility** | Guaranteed same environment everywhere | Depends on host OS, Python version |
| **Size** | ~500 MB (includes Python runtime) | ~100 MB (just packages) |
| **Deployment** | Production-ready; cloud-native | Development-focused |
| **State Persistence** | Requires explicit volume mount | Files stored in host filesystem |

For CI/CD: Docker is standard. For local development: venv is faster to iterate.

---

## Integration with GitHub Actions

Workflow runs container in ephemeral runner:

```yaml
- name: Build Docker image
  run: docker build -t optibot .

- name: Run sync pipeline (Docker)
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    GEMINI_STORE_NAME: ${{ secrets.GEMINI_STORE_NAME }}
  run: |
    docker run --rm \
      -e GEMINI_API_KEY="${GEMINI_API_KEY}" \
      -e GEMINI_STORE_NAME="${GEMINI_STORE_NAME}" \
      -e STATE_FILE=/app/state/state.json \
      -v "${{ github.workspace }}:/app/state" \
      optibot
```

The runner's workspace is mounted as `/app/state`, so `state.json` written by container is available to the subsequent git commit step.

---

## Security Considerations

1. **Secrets Never in Dockerfile**: Environment variables (API keys) are passed at runtime, not baked into image.
2. **Non-Root User** (recommended enhancement):
   ```dockerfile
   RUN useradd -m appuser
   USER appuser
   ```
3. **Minimal Base Image**: Reduces attack surface.

