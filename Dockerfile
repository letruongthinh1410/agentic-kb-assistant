# ── Base image: slim Python 3.11 (smaller attack surface, faster pulls) ──────
FROM python:3.11-slim

# Prevents Python from writing .pyc files and enables unbuffered logging
# (so logs appear in real-time in CI/CD dashboards and Docker output).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Working directory inside the container.
WORKDIR /app

# Install dependencies first (cached layer — only re-runs if requirements.txt changes).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code.
COPY . .

# Create the docs output directory in the image.
RUN mkdir -p /app/docs

# The pipeline runs once and exits.
# Inject secrets via: docker run -e GEMINI_API_KEY=... <image>
CMD ["python", "main.py"]
