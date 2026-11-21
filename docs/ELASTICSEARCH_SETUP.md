# Keyword search (BM25) with Elasticsearch

Pinecone keeps finding chunks **by meaning** (dense vectors). Elasticsearch adds **exact-word matching with BM25**.
Each question runs both searches, and the two ranked lists are merged (reciprocal rank fusion), so a chunk that
both searches like rises to the top.

```
UPLOAD   chunk ──► Gemini embedding ──► Pinecone   (meaning)      same id: "<doc>:<version>:<n>"
              └──► text + permissions ─► Elasticsearch (BM25)

QUESTION question ─► Pinecone top 10 ─┐
                  └► Elasticsearch top 10 ─┴─► merge by rank ─► top 5 ─► mask PII ─► LLM ─► cited sources
```

Your existing Pinecone index is used as it is: no new Pinecone index and no migration are needed.
If Elasticsearch is down, answers still work from Pinecone alone. If `ELASTIC_URL` / `ELASTIC_API_KEY` are empty,
nothing changes from today.

---

## Part 1. In Elastic Cloud (you do this)

> Menu names change now and then; the idea of each step stays the same.

1. **Log in** at <https://cloud.elastic.co> (a free trial is offered; afterwards it is billed).
2. **Create a project.** Pick **one** of the two kinds:
   * **Elasticsearch Serverless project** (simplest): *Create project -> Elasticsearch*, type **General purpose**.
   * **Hosted deployment**: *Create deployment*, the smallest size (1 zone) is enough to start.

   Choose **Amazon Web Services** and a region **close to your EC2 server (Mumbai / ap-south-1 if offered, otherwise
   the nearest, e.g. Singapore)**. Name it `atlas`.
3. **Copy the Elasticsearch endpoint.** It looks like `https://atlas-xxxxxx.es.ap-south-1.aws.elastic.cloud`
   (Serverless: the project's *Connection details* / *Manage*; hosted: *Copy endpoint* next to Elasticsearch).
   This is your `ELASTIC_URL`.
4. **Create an API key** (Serverless: *API keys* in the project; hosted: *Kibana -> Stack Management -> API keys*):
   * Name: `atlas-app`
   * Turn on **Control security privileges** and paste this role (it can only touch the Atlas index):
     ```json
     {
       "atlas_app": {
         "indices": [
           {
             "names": ["atlas-chunks"],
             "privileges": ["create_index", "index", "create", "delete", "read", "view_index_metadata", "maintenance"]
           }
         ]
       }
     }
     ```
   * Click **Create**, then copy the **Encoded** value (a long string). **It is shown only once.** This is your
     `ELASTIC_API_KEY`.
5. **You do not need to create the index.** Atlas creates `atlas-chunks` with the right mapping on first use.

> **Never paste the API key into a chat, an email or the code.** It goes only into your `.env` and a GitHub secret.

---

## Part 2. On your computer

6. Add to `.env` (quotes are tolerated but not needed):
   ```
   ELASTIC_URL=https://atlas-xxxxxx.es.ap-south-1.aws.elastic.cloud
   ELASTIC_API_KEY=the-encoded-key
   ```
7. **Check the connection** (changes nothing):
   ```powershell
   python scripts/sync_to_elasticsearch.py --dry-run
   ```
   It prints `Elasticsearch : Elasticsearch 9.x ... 0 chunks` and how many chunks Pinecone holds.
   * `401` -> the key is wrong or was copied incompletely. `403` -> the role is missing a privilege.
   * A timeout or DNS error -> the URL is wrong.
8. **Load your existing documents** (reads text from Pinecone, writes to Elasticsearch, never changes Pinecone):
   ```powershell
   python scripts/sync_to_elasticsearch.py
   ```
9. **See what keyword search adds** on your own questions:
   ```powershell
   python scripts/compare_keyword.py --show
   ```
   It compares *meaning only*, *keywords only* and *merged*. Use `--questions my_questions.json` for your own
   (`[{"q": "...", "expect": "phrase that must be in the right chunk"}]`).

From now on every new upload is indexed in both places, and deleting a document removes it from both.

---

## Part 3. Production

10. In GitHub (Settings -> Environments -> production, or repository level):
    * **Secret** `ELASTIC_API_KEY` = the encoded key
    * **Variable** `ELASTIC_URL` = the endpoint (and `ELASTIC_INDEX` if you changed the name)
11. Run the deploy (Actions -> Run workflow -> tick **deploy**).
12. On the server, confirm the keyword side is loaded:
    ```bash
    docker exec atlas-rag python scripts/sync_to_elasticsearch.py --dry-run
    ```
    If Elasticsearch holds fewer chunks than Pinecone, run the same command without `--dry-run`.
13. The server's outbound HTTPS (port 443) must be allowed; the default EC2 security group already allows it.

**Switching it off / rolling back:** remove `ELASTIC_URL`, or set `KEYWORD_BACKEND=none`, and redeploy. Search goes
back to Pinecone only. Nothing is deleted.

---

## Optional: a local Elasticsearch for development (alongside Elastic Cloud)

You can keep a local Elasticsearch on your computer for testing, and use Elastic Cloud for production. Each place
has its own settings, so they never mix: **your `.env` decides what runs on your computer, and GitHub decides what
runs on the server.**

| | Local development | Production (EC2) |
|---|---|---|
| Elasticsearch | Docker container on your computer | Elastic Cloud |
| Where the URL is set | `.env` | GitHub (`ELASTIC_URL`) |
| API key | none | `ELASTIC_API_KEY` (GitHub secret) |

1. Start Docker Desktop, then from the project root:
   ```powershell
   docker compose -f docker-compose.elastic.yml up -d
   curl http://localhost:9200            # should print some JSON with a version number
   ```
2. In `.env` (leave the key empty; an `http://` URL does not need one):
   ```
   ELASTIC_URL=http://localhost:9200
   ELASTIC_API_KEY=
   ```
3. Load and compare, exactly as for the cloud:
   ```powershell
   python scripts/sync_to_elasticsearch.py
   python scripts/compare_keyword.py --show
   ```
4. To use the cloud from your computer instead, put the cloud URL and key in `.env`. Keep both blocks in the file
   and comment out the one you are not using.

Good to know:
* The local container runs **without security**, so its port is published on `127.0.0.1` only. Never expose it.
* Local data is separate from the cloud's. Run the sync script once for each.
* The production server cannot reach your computer, so it always uses Elastic Cloud.
* The app inside a Docker container on your own machine reaches it at `http://host.docker.internal:9200`.
* Stop it with `docker compose -f docker-compose.elastic.yml down` (add `-v` to delete the data).
* It uses about 1 GB of memory; stop it when you are not testing.

---

## Self-hosted on the EC2 server (instead of Elastic Cloud)

If you do not set `ELASTIC_URL`, the deploy runs Elasticsearch itself **on the same server, next to the app**:

* A container `atlas-elasticsearch` (version 8.17.0, 512 MB heap, 1 GB memory limit) joins a private Docker network
  `atlas-net` together with the app. It is **not published** on any port, so nothing outside the server can reach it.
* The app gets `ELASTIC_URL=http://atlas-elasticsearch:9200` automatically (no API key is needed on a private network).
* Its data lives in the Docker volume `atlas-es-data`, so it survives deploys and restarts. A deploy replaces only the app.
* After the app is healthy, the deploy loads the documents already in Pinecone into it (the sync is repeatable).

**Safety check before it starts.** Elasticsearch needs about **1.3 GB of free memory and 3 GB of free disk**. If the
server has less, the step is **skipped** with a message in the run summary, and the app is deployed anyway
(questions are answered from Pinecone alone). So a small server never breaks the site, but it also gets no keyword
search. A 4 GB instance (for example `t3.medium`) and a larger disk are comfortable.

| Setting (GitHub variable) | Effect |
|---|---|
| `ELASTIC_URL` set (Elastic Cloud) | Nothing is started on the server; the cloud is used |
| `SELF_HOST_ELASTICSEARCH=false` | Do not start Elasticsearch on the server |
| neither | Start it on the server if there is room |

To see what happened, open the deploy run and read its summary: it says *started and healthy*, *already running*, or
*SKIPPED* with the numbers. Useful commands on the server:
```bash
docker ps --filter name=atlas-elasticsearch
docker exec atlas-elasticsearch curl -s localhost:9200/_cluster/health
docker exec atlas-rag python scripts/sync_to_elasticsearch.py --dry-run
```

---

## Settings
| Name | Default | Meaning |
|---|---|---|
| `ELASTIC_URL` | empty | Elasticsearch endpoint. Empty = not used |
| `ELASTIC_API_KEY` | empty | The *Encoded* API key |
| `ELASTIC_INDEX` | `atlas-chunks` | Index name (must match the API key's role) |
| `ELASTIC_MIN_SCORE` | `1.0` | A keyword hit needs at least this BM25 score |
| `ELASTIC_MIN_RATIO` | `0.3` | ... and at least this fraction of the best hit's score |
| `RRF_KEYWORD_WEIGHT` | `1.3` | How much the keyword list counts in the merge (the meaning list is `RRF_DENSE_WEIGHT`, `1.0`). On the sample documents 1.25 to 1.5 found the right chunk first for 12 of 12 questions; equal weights (1.0) managed 8 of 12 |
| `KEYWORD_BACKEND` | `auto` | `auto`: Elasticsearch if configured. `none`: off. `pinecone`: use Pinecone's sparse vectors instead |

## How it is stored
One Elasticsearch document per chunk, with the same id as its Pinecone vector. Fields: `tenant_id`, `doc_id`,
`title`, `text`, `page`, `chunk`, `version`, `allowed_groups`, `owner_sub`. `text` is analysed twice: with the
English analyzer (so "leaves" finds "leave") and with the standard analyzer (so a code like `ZX-4471` matches as
written). Every query filters on `tenant_id` and on who may read the chunk, exactly like the Pinecone search.

## Good to know
* **Data location:** Elasticsearch stores the chunk text (as Pinecone does in its metadata). Documents that contain
  personal data are still hidden in the app (no excerpt, no PDF link), and the text is masked before it reaches the LLM.
* **Tuning:** if unrelated questions return keyword hits, raise `ELASTIC_MIN_SCORE` (e.g. 2.0) or `ELASTIC_MIN_RATIO`;
  if good answers are missed, lower them. Use `compare_keyword.py --show` to see the effect.
* **Repair:** if Elasticsearch was down during an upload, run `python scripts/sync_to_elasticsearch.py`.
* **Tests:** `python test/test_keyword.py` (23 offline tests, also run in the pipeline).
