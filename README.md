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

## Build & push to ECR

```bash
ACCOUNT=007924090369
REGION=eu-north-1
REPO=binbon/demo
TAG=v1

# 1. authenticate docker to ECR
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

# 2. build (create the repo first, in console or Terraform)
docker build -t $REPO:$TAG .

# 3. tag + push
docker tag $REPO:$TAG $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG
```

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
