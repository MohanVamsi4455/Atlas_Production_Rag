# Atlas CI/CD setup (GitHub Actions + Amazon ECR)

Workflow file: [`.github/workflows/cicd.yaml`](../.github/workflows/cicd.yaml)

```
push to main ──> 1. integration ──> 2. build-push ──> 3. deploy (optional)
                 compile + smoke     image -> ECR        EC2 pulls and runs it
pull request ──> 1. integration only (nothing is pushed or deployed)
```

| Job | What it does | Needs |
|---|---|---|
| **integration** | Installs the libraries, checks every Python file compiles, starts the app and checks PII masking and the guardrails | Nothing (no cloud accounts) |
| **build-push** | Builds the Docker image and pushes it to ECR, tagged with the commit id and `latest`. Creates the ECR repository if missing | AWS user + 3 settings |
| **deploy** | On your EC2 server: pulls the image, replaces the container, waits for it to be healthy | EC2 + self-hosted runner. **Off until you enable it** |

You can use the first two jobs right away; the deploy job stays skipped until you set `DEPLOY_TO_EC2 = true`.

---

## Step 1. Create an AWS user that may push to ECR

IAM → Users → Create user (e.g. `github-actions-atlas`) → **Create access key** (use case: "Application running outside AWS").
Attach this policy (replace `REGION` and `ACCOUNT_ID`; `atlas-rag` is the repository name you choose in Step 3):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:DescribeRepositories", "ecr:CreateRepository", "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
        "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "arn:aws:ecr:REGION:ACCOUNT_ID:repository/atlas-rag"
    }
  ]
}
```

---

## Step 2. Add the GitHub secrets

Repository → **Settings → Secrets and variables → Actions → Secrets → New repository secret**.
Paste each value **without quotes**.

> **Environment secrets also work.** If you store them under *Settings → Environments → production* instead, the build-push and deploy jobs can read them, because both declare `environment: production`. A secret may also be used for the settings listed as variables below; the workflow looks for a variable first, then a secret with the same name.

| Secret | Needed for | Value |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | build-push, deploy | Access key from Step 1 |
| `AWS_SECRET_ACCESS_KEY` | build-push, deploy | Secret key from Step 1 |
| `GEMINI_API_KEY` | deploy | Same as in your `.env` |
| `GROQ_API_KEY` | deploy | Same as in your `.env` |
| `PINECONE_API_KEY` | deploy | Same as in your `.env` |
| `DATABASE_URL` | deploy | The Render PostgreSQL URL from your `.env` |
| `RIUS_API_KEY` | deploy (optional) | Same as in your `.env`, or leave out |
| `APP_AWS_ACCESS_KEY_ID`, `APP_AWS_SECRET_ACCESS_KEY` | deploy (optional) | Keys the *app* uses for S3. Skip them if the EC2 server has an IAM role |

---

## Step 3. Add the GitHub variables

Same page, **Variables** tab → **New repository variable**. These are not secret. **No quotes.**

| Variable | Example / value | Needed for |
|---|---|---|
| `AWS_REGION` | `ap-south-1` | build-push, deploy |
| `ECR_REPOSITORY_NAME` | `atlas-rag` (just the name, not a URL) | build-push |
| `S3_BUCKET` | Your bucket name | deploy |
| `GEMINI_CHAT_MODELS` | Same as in your `.env` | deploy |
| `GROQ_CHAT_MODELS` | Same as in your `.env` | deploy |
| `PINECONE_INDEX_NAME` | Same as in your `.env` | deploy |
| `DEPLOY_TO_EC2` | `true` to enable the deploy job (a variable or a secret, in the repository or the `production` environment) | deploy |
| Optional: `CHAT_PROVIDER_ORDER`, `GEMINI_EMBED_MODEL`, `EMBED_DIM`, `PINECONE_CLOUD`, `PINECONE_REGION`, `PINECONE_READY_TIMEOUT`, `RIUS_SERVICE_NAME`, `RIUS_DISABLED`, `RIUS_CAPTURE_CONTENT`, `MAX_UPLOAD_BYTES`, `MIN_RELEVANCE`, `HITL_ENABLED`, `MEMORY_TURNS`, `PII_MODE` | Only set the ones you want to change; anything left out uses the app's default | deploy |

---

## Step 4. Run it

Push to `main`, or open **Actions → Atlas CI/CD → Run workflow**.
When `build-push` finishes you will see the image in **ECR → Repositories → atlas-rag**, tagged with the commit id and `latest`.

---

## Step 5 (optional). Deploy to EC2

1. Launch an Amazon Linux or Ubuntu EC2 instance. Install **Docker** and the **AWS CLI**. Open port **8000** in its security group (or put a proxy such as nginx in front).
2. Give the instance an **IAM role** that allows `ecr:GetAuthorizationToken` plus pulling from your repository, and access to your S3 bucket. Then you can skip the `APP_AWS_*` secrets.
3. Register the server as a runner: repository **Settings → Actions → Runners → New self-hosted runner** (Linux, x64). Follow the commands shown, add the label **`atlas-ec2`** when asked, and install it as a service (`sudo ./svc.sh install && sudo ./svc.sh start`).
4. Let the runner use Docker: `sudo usermod -aG docker <runner-user>`, then restart the runner service.
5. Set the variable `DEPLOY_TO_EC2 = true`. The next run deploys, then waits until the container reports **healthy**.

Check it: `http://<ec2-public-ip>:8000`

Roll back to an earlier version (commit ids are the image tags), on the EC2 server:
```bash
docker rm -f atlas-rag
docker run -d --name atlas-rag --restart unless-stopped -p 8000:8000 --env-file <your-env-file> ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/atlas-rag:<commit-id>
```
(For a hand-made env file, remove the quotes around values, because `--env-file` keeps them.)

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `integration` fails at the smoke test | Read the assertion message; it names the check that broke (PII masking, a guardrail, or the app not starting) |
| `Could not load credentials` / `not authorized` | Secrets missing or mistyped, or the IAM policy does not match the region, account or repository name |
| `Missing required secret or variable: X` | Add `X` in Step 2 or 3 (deploy job only) |
| Deploy job waits forever | No runner with the label `atlas-ec2` is online. Check **Settings → Actions → Runners** |
| Container exits at once on EC2 | `docker logs atlas-rag`. A setting was passed with quotes or is wrong |
| `build-push` skipped | It does not run on pull requests, only on pushes to `main` and manual runs |
