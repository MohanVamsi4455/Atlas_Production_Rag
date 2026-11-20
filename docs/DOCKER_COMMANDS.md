# Atlas: Docker commands

Run every command from the **project root** (`D:\Mohan_Vamsi\Projects\Atlas_Production_Rag`).
Examples use PowerShell. Where bash differs, it is noted.

Image name: `atlas-rag` · Container name: `atlas-rag` · App port: `8000`

---

## 0. Before you start

Docker Desktop must be **running** (whale icon in the tray, "Engine running").

```powershell
docker version          # both "Client" and "Server" sections must appear
```

If you see `failed to connect to the docker API … dockerDesktopLinuxEngine`, Docker Desktop is not started yet.

---

## 1. Secrets: how the container gets your `.env`

The image never contains `.env` (it is listed in `.dockerignore`). Give it to the container at run time.

**Use the bind-mount (recommended).** It reads your `.env` exactly as the app does outside Docker:

```powershell
-v "${PWD}/.env:/srv/.env:ro"
```

**Do not use `--env-file .env` with this project.** Docker keeps quotes literally, so
`PINECONE_INDEX_NAME="atlas-rag-index"` becomes a value that *includes* the quote marks and breaks.
If you must use `--env-file`, remove the quotes around every value first.

On a hosting platform (Render, ECS, ...) set the variables in the platform's settings instead.

---

## 2. Build the image

```powershell
docker build -t atlas-rag:latest .
```

Rebuild without cache (after changing `requirements.txt` oddly, or to start clean):

```powershell
docker build --no-cache -t atlas-rag:latest .
```

Check it:

```powershell
docker images atlas-rag
```

---

## 3. Run

Background, auto-restart, `.env` mounted read-only:

```powershell
docker run -d --name atlas-rag -p 8000:8000 -v "${PWD}/.env:/srv/.env:ro" --restart unless-stopped atlas-rag:latest
```

Open **http://localhost:8000** · API docs: **http://localhost:8000/api/docs**

Port 8000 already in use? Map another host port (left side):

```powershell
docker run -d --name atlas-rag -p 8080:8000 -v "${PWD}/.env:/srv/.env:ro" atlas-rag:latest
```

Foreground (see logs live, Ctrl+C to stop, container removed afterwards):

```powershell
docker run --rm -it --name atlas-rag -p 8000:8000 -v "${PWD}/.env:/srv/.env:ro" atlas-rag:latest
```

> bash / Git Bash: use `$(pwd)` instead of `${PWD}`, e.g. `-v "$(pwd)/.env:/srv/.env:ro"`.

---

## 4. Status, logs, health

```powershell
docker ps                                   # running containers
docker ps -a                                # including stopped ones
docker logs -f atlas-rag                    # follow logs (Ctrl+C to leave)
docker logs --tail 100 atlas-rag            # last 100 lines
docker inspect --format "{{.State.Health.Status}}" atlas-rag   # starting | healthy | unhealthy
docker stats atlas-rag                      # live CPU / memory
curl http://localhost:8000/health/live      # {"status":"alive"}
curl http://localhost:8000/health/ready     # checks the database
```

---

## 5. Run the project scripts inside the container

```powershell
docker exec atlas-rag python scripts/init_db.py            # create / update database tables (safe to rerun)
docker exec atlas-rag python scripts/check_connections.py  # S3, Groq, Gemini, Pinecone, embeddings
docker exec atlas-rag python scripts/backfill_pii.py       # scan older uploads for personal data
```

Open a shell inside it:

```powershell
docker exec -it atlas-rag bash
```

---

## 6. Stop, start, restart, remove

```powershell
docker stop atlas-rag
docker start atlas-rag
docker restart atlas-rag
docker rm -f atlas-rag          # stop and delete the container (the image stays)
```

---

## 7. After you change the code: rebuild and replace (one go)

```powershell
docker rm -f atlas-rag; docker build -t atlas-rag:latest .; docker run -d --name atlas-rag -p 8000:8000 -v "${PWD}/.env:/srv/.env:ro" --restart unless-stopped atlas-rag:latest
```

Changing only `.env`? No rebuild needed: `docker restart atlas-rag`.

---

## 8. Development mode: live code reload without rebuilding

Mount your source folders so edits show up immediately, and run uvicorn with `--reload`:

```powershell
docker run --rm -it --name atlas-dev -p 8000:8000 `
  -v "${PWD}/.env:/srv/.env:ro" `
  -v "${PWD}/app:/srv/app" -v "${PWD}/worker:/srv/worker" `
  -e WATCHFILES_FORCE_POLLING=true `
  atlas-rag:latest uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

`WATCHFILES_FORCE_POLLING` is needed because file-change events do not cross from Windows into the container.

---

## 9. Clean up disk space

```powershell
docker system df                # how much space Docker uses
docker image prune              # remove dangling (untagged) images
docker rmi atlas-rag:latest     # remove this image
docker system prune             # remove stopped containers, unused networks, dangling images
```

---

## 10. Publish the image (optional)

Docker Hub:

```powershell
docker login
docker tag atlas-rag:latest YOUR_DOCKERHUB_USER/atlas-rag:1.0
docker push YOUR_DOCKERHUB_USER/atlas-rag:1.0
```

AWS ECR (replace the account id and region):

```powershell
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com
docker tag atlas-rag:latest ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/atlas-rag:1.0
docker push ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/atlas-rag:1.0
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `failed to connect to the docker API` | Start Docker Desktop and wait for "Engine running". |
| `port is already allocated` | Another app uses 8000. Use `-p 8080:8000`, or stop the other one (`docker ps`). |
| `container name "/atlas-rag" is already in use` | `docker rm -f atlas-rag`, then run again. |
| Container exits at once | `docker logs atlas-rag`. A missing or unreadable `.env` mount is the usual cause. |
| Pinecone index error with quotes in the name | You used `--env-file`. Use the `.env` bind-mount instead (section 1). |
| `health` shows `unhealthy` | `docker logs atlas-rag`; also check `/health/ready` for database problems. |
| Code changes not showing | Rebuild (section 7) or use development mode (section 8). |
