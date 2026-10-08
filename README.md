# Binbon

A small photo-sharing web app whose real purpose is to **prove the AWS infra is wired correctly**.
Users sign up, log in and upload photos. Every action touches a real AWS service:

| Feature                         | Service                                             |
|---------------------------------|-----------------------------------------------------|
| Accounts + photo metadata       | Aurora PostgreSQL (`users`, `photos` tables, auto-created) |
| Login sessions, upload counter  | ElastiCache Redis/Valkey                            |
| Photo files                     | S3 (`photos/<user_id>/<photo_id>.<ext>`, bucket stays private — the app streams images) |
| `/status` page                  | Live check of DB, Redis, S3 and Secrets Manager     |

## Pages

| Path          | What it does                                                 |
|---------------|--------------------------------------------------------------|
| `/`           | Landing page                                                 |
| `/signup`     | Create an account                                            |
| `/login`      | Log in (session cookie, stored in Redis)                     |
| `/dashboard`  | Upload form (drag & drop, up to 10 images, JPEG/PNG/GIF/WebP) + gallery |
| `/status`     | Green/red health of every backing service                    |

Code layout: `app/web.py` (UI), `app/api.py` (JSON checks below), `app/auth.py`
(users + sessions), `app/config.py` (env vars + clients), `app/templates/`, `app/static/`.

## JSON infra-check endpoints → what each one proves

The list is also available at `GET /api`.

| Method + path        | Proves                                                        |
|----------------------|--------------------------------------------------------------|
| `GET /health`        | ALB → Fargate routing + health checks + CloudWatch logs      |
| `GET /config`        | Which env vars got injected (no secret values shown)         |
| `GET /db`            | Aurora PostgreSQL connectivity (`SELECT version()`)          |
| `POST /db`           | Postgres write + read back (creates `demo_items`)            |
| `POST /cache`        | ElastiCache Redis/Valkey `SET`                               |
| `GET /cache/{key}`   | Redis `GET`                                                  |
| `POST /upload`       | S3 `put_object` via the **task role** (no keys)             |
| `GET /download/{key}`| S3 `get_object`                                              |
| `GET /s3`            | S3 `list_objects_v2`                                         |
| `GET /secret`        | Secrets Manager `get_secret_value` (returns keys only)       |

> `/health` is **dependency-free on purpose**. If it checked the DB, the ALB would never
> mark the task healthy while the DB was still starting, and the task would be killed in a loop.

## Environment variables (injected by the ECS task definition)

| Variable            | Example / note                                             |
|---------------------|-----------------------------------------------------------|
| `AWS_REGION`        | `eu-north-1`                                               |
| `DB_HOST`           | Aurora cluster writer endpoint                            |
| `DB_PORT`           | `5432`                                                     |
| `DB_NAME`           | `binbon`                                                   |
| `DB_USER`           | `binbon_app`                                               |
| `DB_SECRET_ARN`     | **preferred**: secret with `{"username","password"}` (e.g. Aurora's managed master secret). Fetched at runtime and re-read automatically after rotation |
| `DB_PASSWORD`       | fallback only, used when `DB_SECRET_ARN` is unset        |
| `REDIS_HOST`        | ElastiCache endpoint                                       |
| `REDIS_PORT`        | `6379`                                                     |
| `REDIS_TLS`         | `true` if in-transit encryption is on                     |
| `REDIS_AUTH_TOKEN`  | optional; from Secrets Manager if used                    |
| `S3_BUCKET`         | your delivery/test bucket name                            |
| `DEMO_SECRET_ARN`   | secret read by `/secret` + status page (defaults to `DB_SECRET_ARN`) |
| `COOKIE_SECURE`     | `true` behind an HTTPS ALB listener (default `false`)     |
| `MAX_UPLOAD_MB`     | max size per photo (default `10`)                          |

## Deploying to AWS: the big picture

The infrastructure (VPC, ALB, ECS, ElastiCache, S3, Secrets Manager, IAM, including the
GitHub Actions role) lives in the sibling repo **`binbon-infra`**. Its README explains every
Terraform block and how to build the same thing in the AWS console.

The order is always:

1. **Create the infrastructure and push a first image** (bootstrap), using `./deploy.sh up` in
   `binbon-infra`, or by hand in the console. The ECS service needs *some* image to start with.
2. **From then on, every push to `main` deploys automatically** through GitHub Actions (next section).

Building and pushing the bootstrap image by hand:

```bash
ACCOUNT=007924090369
REGION=eu-north-1
REPO=binbon/demo
TAG=v1

# 1. authenticate docker to ECR
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

# 2. build for the CPU the Fargate task uses (X86_64), even on an ARM laptop
docker build --platform linux/amd64 -t $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG .

# 3. push (the ECR repo must already exist)
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG
```

## CI/CD pipeline (GitHub Actions)

The pipeline is one file: [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml).
GitHub reads it and runs it on its own machines whenever its trigger happens.

### Basic terms

| Term | Meaning |
|---|---|
| **Workflow** | The whole YAML file: an automated process |
| **Trigger** (`on:`) | What starts it: here a push to `main`, or the manual **Run workflow** button (`workflow_dispatch`) |
| **Job** | A group of steps that runs on one machine. We have one job: `deploy` |
| **Runner** | The machine running the job. `runs-on: ubuntu-latest` = a fresh GitHub-hosted Ubuntu VM per run, deleted afterwards. **Free for public repos** (2,000 min/month for private); a run takes about 3–4 min |
| **Step** | One command (`run:`) or one reusable **action** (`uses:`) |
| **Action** | A ready-made step published by someone, e.g. `aws-actions/configure-aws-credentials` |

### The file, section by section

```yaml
on:
  push:
    branches: [main]      # every push/merge to main deploys
  workflow_dispatch:      # adds a "Run workflow" button in the Actions tab
```

```yaml
permissions:
  id-token: write         # lets the job request a signed OIDC token from GitHub (to log in to AWS)
  contents: read          # lets the job read (check out) the code; nothing more
```
The job's built-in `GITHUB_TOKEN` gets only these permissions (least privilege).

```yaml
concurrency:
  group: deploy-dev
  cancel-in-progress: false
```
Two quick pushes never deploy at the same time. The second run waits for the first one to finish.

```yaml
env:
  AWS_REGION: eu-north-1
  AWS_ROLE_ARN: arn:aws:iam::007924090369:role/binbon-dev-github-actions
  ECR_REPOSITORY: binbon/demo
  ECS_CLUSTER: binbon-dev-cluster
  ECS_SERVICE: binbon-dev-demo
  TASK_FAMILY: binbon-dev-demo
  CONTAINER_NAME: demo
```
The names of the AWS resources created by `binbon-infra`. If you rename anything there, change it here.
None of these values are secret, so no GitHub Secrets are needed at all.

### The steps

| # | Step | What it does | Why |
|---|---|---|---|
| 1 | `actions/checkout@v4` | Copies the repo onto the runner | The Docker build needs the code |
| 2 | **Configure AWS credentials (OIDC)**: `aws-actions/configure-aws-credentials@v4` | Gets a signed token from GitHub saying *"repo binbon-demo, branch main"*, sends it to AWS STS, and receives **temporary credentials (about 1 hour)** for `binbon-dev-github-actions` | **No AWS access keys stored in GitHub.** AWS accepts the token only if it matches the role's trust policy (this repo, this branch) |
| 3 | **Log in to ECR**: `aws-actions/amazon-ecr-login@v2` | Runs `docker login` against the account's ECR registry and outputs the registry URL | Needed before `docker push` |
| 4 | **Build and push image** | `docker build --platform linux/amd64` and `docker push`, tagged **`<registry>/binbon/demo:<commit SHA>`** | A unique tag per commit: you always know exactly which code is running, and you can roll back to any earlier one |
| 5 | **Fetch current task definition** | `aws ecs describe-task-definition` → `task-definition.json` | Starts from the **live** definition, so the env vars, roles, CPU/memory and logging set by Terraform are kept |
| 6 | **Set new image**: `aws-actions/amazon-ecs-render-task-definition@v1` | Replaces only the `image` of container `demo` in that JSON | Changes nothing except the image |
| 7 | **Deploy**: `aws-actions/amazon-ecs-deploy-task-definition@v2` | Registers the JSON as a **new revision** (`binbon-dev-demo:N+1`), points the service at it, and with `wait-for-service-stability: true` **waits** until the new task is running and **healthy behind the ALB** | ECS does a rolling update: the old task keeps serving until the new one passes `/health`. If the new task never becomes healthy, the run fails (red) and ECS keeps the old version running |

### What the pipeline is allowed to do in AWS

Only what the role `binbon-dev-github-actions` allows (defined in `binbon-infra/github.tf`):
push to **this** ECR repository, register task definitions, update **this one** ECS service,
and pass **only** the two ECS roles. It cannot read S3, the DB secret, or anything else.

### Setting it up from scratch

1. AWS side: create the OIDC provider and the role (Terraform `github.tf`, or console steps in
   `binbon-infra/README.md`, section 6.13). The trust policy's `sub` must be
   `repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:refs/heads/main`. Get the IDs with
   `curl -s https://api.github.com/repos/<owner>/<repo>` (`owner.id` and `id`).
2. GitHub side: nothing to configure. Actions is enabled by default; committing the workflow file is enough.
3. Pushing a workflow file needs a token with **Contents** *and* **Workflows: Read and write**.

### Daily use

- **Deploy:** `git push origin main`, then watch **Actions → Build and deploy**.
- **Re-deploy without code changes:** Actions → Build and deploy → **Run workflow**.
- **See what's running:** ECS → `binbon-dev-cluster` → `binbon-dev-demo` → task definition, whose image tag is the commit SHA.
- **Roll back** to an earlier revision (image):
  ```bash
  aws ecs update-service --cluster binbon-dev-cluster --service binbon-dev-demo \
    --task-definition binbon-dev-demo:<older-revision>
  ```
  Or in the console: service → **Update** → choose the older revision.

### Troubleshooting

| Failed step | Likely cause |
|---|---|
| Configure AWS credentials: `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy doesn't match the token, or the infra is destroyed. CloudTrail → `AssumeRoleWithWebIdentity` shows the exact `sub` GitHub sent |
| Build and push: `denied` / `not authorized` | The role's ECR permissions, or the repo name in `env:` is wrong |
| Fetch task definition: `Unable to describe task definition` | The infra isn't deployed (`./deploy.sh up` first) |
| Deploy: `AccessDeniedException ... iam:PassRole` | The role may not pass the ECS roles (`PassEcsRoles` statement) |
| Deploy: times out waiting for stability | The new task isn't healthy. Check **CloudWatch → `/ecs/binbon-demo`** and the service's **Events** tab. ECS keeps the old version serving |

## IAM the ECS roles need

**Execution role** (used by the ECS agent, not your code):
- `AmazonECSTaskExecutionRolePolicy` (pull image from ECR + write logs)
- (only if you inject env vars via the task definition `secrets` block: `secretsmanager:GetSecretValue` on those ARNs)

**Task role** (used by this app at runtime):
- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` on your bucket (+ `/*`)
- `secretsmanager:GetSecretValue` on `DB_SECRET_ARN` (+ `DEMO_SECRET_ARN` if different)
- `kms:Decrypt` on the secret's KMS key, if it uses a customer-managed key

## Test locally

```bash
docker compose up --build      # then open http://localhost:8000
```

This brings up the app with Postgres, Redis and LocalStack (S3 + Secrets Manager).
**`docker-compose.yml` contains no passwords:**

1. `localstack-init` generates a random DB password on first run, saves it to the
   `db-secret` volume and stores `{"username","password"}` in LocalStack Secrets Manager as `binbon/db`.
2. `postgres` initialises from that file via `POSTGRES_PASSWORD_FILE`.
3. The app gets only `DB_SECRET_ARN=binbon/db` and fetches the credentials at runtime,
   the same code path it uses against Aurora in AWS.

`docker compose down` keeps the data and password; `docker compose down -v` wipes both
(a new password is generated next time). The only local secret is `LOCALSTACK_AUTH_TOKEN`
in `.env`, which is git-ignored.
