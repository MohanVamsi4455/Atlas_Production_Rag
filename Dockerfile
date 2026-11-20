# ---------------------------------------------------------------------------
# Atlas Dockerfile: packages the FastAPI app into an image you can run anywhere.
# Build:  docker build -t atlas-rag:latest .
# ---------------------------------------------------------------------------

# Base image: a small official Python 3.12 install on Debian ("slim" = no extra tools, so the image stays small).
FROM python:3.12-slim

# Environment variables set for the whole image:
#   PYTHONDONTWRITEBYTECODE=1  -> do not create .pyc cache files (keeps the image clean)
#   PYTHONUNBUFFERED=1         -> print logs immediately, so `docker logs` shows them live
#   PIP_NO_CACHE_DIR=1         -> pip keeps no download cache, which makes the image smaller
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Folder inside the container where the app lives. Every later command runs from here,
# and the app reads its ".env" file from this folder (/srv/.env).
WORKDIR /srv

# Install the libraries FIRST, before copying the code. Docker caches each step, so this slow
# step is only redone when requirements.txt changes, not on every small code edit.
COPY requirements.txt .

# 1) pip install: downloads every library listed in requirements.txt (FastAPI, Pinecone, boto3, ...).
# 2) useradd: creates a normal, non-admin user named "atlas" (id 10001) to run the app as.
RUN pip install -r requirements.txt && useradd -r -u 10001 atlas

# Copy the project folders into the image. Secrets are NOT copied: .env is blocked by .dockerignore
# and is supplied when the container starts (see docs/DOCKER_COMMANDS.md).
COPY app ./app
COPY worker ./worker
# infra/ holds schema.sql, which scripts/init_db.py uses to create the database tables.
COPY infra ./infra
# scripts/ holds helper tools you can run inside the container (init_db.py, check_connections.py, ...).
COPY scripts ./scripts

# From here on, run as the non-admin "atlas" user. If the app were ever compromised,
# the attacker would not have root rights inside the container.
USER atlas

# Documentation only: tells people the app listens on port 8000 inside the container.
# It does not open the port; that is done with `-p 8000:8000` in `docker run`.
EXPOSE 8000

# Docker runs this check every 30 seconds to learn whether the app is really working:
#   --start-period=25s  -> give the app 25 seconds to start before failures count
#   --timeout=5s        -> the check fails if there is no answer within 5 seconds
#   --retries=3         -> the container is marked "unhealthy" after 3 failures in a row
# The command asks the app's /health/live page and succeeds only if it answers with HTTP 200.
# See the result with: docker inspect --format "{{.State.Health.Status}}" atlas-rag
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"

# The command that starts the app when the container runs:
#   uvicorn           -> the web server that runs FastAPI
#   app.main:app      -> "the 'app' object inside app/main.py"
#   --host 0.0.0.0    -> accept connections from outside the container (127.0.0.1 would only work inside it)
#   --port 8000       -> the port to listen on
#   --proxy-headers   -> trust the real client address when the app sits behind a proxy or load balancer
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
