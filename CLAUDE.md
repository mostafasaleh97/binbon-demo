# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Binbon is a small FastAPI photo-sharing app. Its real purpose is to exercise and validate AWS infrastructure (ECS Fargate behind an ALB, Aurora PostgreSQL, ElastiCache Redis/Valkey, S3, Secrets Manager). This repo holds only the application code. The Terraform infra and the CI/CD pipeline (build → ECR → ECS) are planned separately. The same image must run unchanged locally and in Fargate, so **all configuration comes from env vars** (`app/config.py`).

## Commands

```bash
docker compose up -d --build          # build + run full local stack → http://localhost:8000
docker compose up -d --build app      # rebuild/restart only the app after code changes
docker compose logs -f app
docker compose down                   # stop; keeps Postgres data + generated DB password
docker compose down -v                # wipe everything (new DB password generated next start)

# inspect LocalStack (the region matters: resources live in eu-north-1, awslocal defaults to us-east-1)
docker compose exec localstack awslocal --region eu-north-1 s3 ls s3://binbon-demo --recursive
docker compose exec localstack awslocal --region eu-north-1 secretsmanager get-secret-value --secret-id binbon/db
docker compose exec postgres psql -U postgres -d binbon
```

There is no test suite or linter. Verify changes by rebuilding and exercising the app: the `/status` page shows a live check of each backing service, and the JSON endpoints (`/health`, `/db`, `/cache`, `/s3`, `/secret`, listed at `/api`) can be hit with curl. For the UI flows, use a cookie jar: `curl -c cj -d 'name=..&email=..&password=..&confirm_password=..' localhost:8000/signup`, then `curl -b cj -F photos=@img.png localhost:8000/photos`.

## Architecture

- `app/main.py` assembles the FastAPI app: it mounts `/static`, includes both routers, and adds a global exception handler (HTML error page for browsers, JSON otherwise).
- `app/config.py` is the single source of env config plus shared clients: boto3 `s3`/`sm`, a Redis connection pool, `db_conn()`, and the lazily created DB schema (`ensure_schema()`). There is no migration tool. Schema changes go in `SCHEMA_SQL` as idempotent `CREATE ... IF NOT EXISTS`, and any code path that touches `users`/`photos` must call `ensure_schema()` first.
- `app/api.py` holds the JSON infra-check endpoints. Each one tests exactly one service.
- `app/web.py` holds the HTML UI (Jinja2 templates in `app/templates/`): sign-up/log-in, the photo dashboard, and `/status`.
- `app/auth.py`: users live in Postgres (PBKDF2 hashes, stdlib only). Sessions are random tokens stored in Redis as `session:<token>` → user JSON, held in the `binbon_session` cookie. Keeping sessions in Redis lets Fargate tasks scale without sticky sessions.

Data placement: photo bytes live in S3 at `photos/<user_id>/<photo_id>.<ext>`. Metadata lives in the Postgres `photos` table. Redis holds sessions plus the `stats:uploads` counter. The bucket stays private. Images are streamed through `GET /photos/{id}` with an ownership check, so don't switch to presigned or public URLs. Presigned URLs would also break locally, because the LocalStack hostname isn't reachable from the browser. Uploads are validated by magic bytes (`sniff_image`), not by the client-supplied content type.

## Invariants to preserve

- **`/health` must stay dependency-free.** The ALB health check uses it. If it depended on the DB, tasks would be killed in a loop while the DB starts. The Dockerfile `HEALTHCHECK` hits it too.
- **No access keys in code.** boto3 uses the ECS task role. `AWS_ENDPOINT_URL` (LocalStack) is set only in compose and must stay unset on AWS. S3 uses path-style addressing so the same client works against both.
- **No passwords in `docker-compose.yml`.** DB credentials come from Secrets Manager via `DB_SECRET_ARN` (`{"username","password"}`, the same shape as Aurora's managed master secret). `db_credentials()` caches them and re-reads the secret once on `password authentication failed`, which handles rotation. `DB_PASSWORD` is only a fallback when no ARN is set. Locally, the `localstack-init` one-shot container generates the password once into the `db-secret` volume. Postgres reads it via `POSTGRES_PASSWORD_FILE`, and the init container re-seeds LocalStack (which is ephemeral) from that file on every start. If you change the Postgres password by hand, keep the volume file in sync, or the next restart will push the stale value back into the secret.
- `.env` holds the real `LOCALSTACK_AUTH_TOKEN`. It is git-ignored and docker-ignored.

## AWS deployment notes

- Task role: `s3:GetObject/PutObject/DeleteObject/ListBucket` on the bucket, plus `secretsmanager:GetSecretValue` on `DB_SECRET_ARN` (and `kms:Decrypt` if the secret uses a CMK).
- Set `COOKIE_SECURE=true` behind an HTTPS listener.
- The README has the ECR build/push commands (account `007924090369`, region `eu-north-1`, repo `binbon/demo`) and the full env-var table.
