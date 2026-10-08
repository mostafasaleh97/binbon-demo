"""
Shared configuration and clients.

Everything comes from environment variables (injected by the ECS task
definition), so the same image runs locally against docker compose and in
Fargate against Aurora / ElastiCache / S3 / Secrets Manager with no code changes.
"""

import os
import json
import logging
import datetime

import boto3
from botocore.config import Config
import psycopg2
import redis

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("binbon")

AWS_REGION = os.getenv("AWS_REGION", "eu-north-1")

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")  # fallback only; prefer DB_SECRET_ARN
# Name/ARN of a Secrets Manager secret holding {"username": ..., "password": ...}
# (the same shape as Aurora's managed master-user secret). When set, the DB
# credentials are fetched at runtime and never appear in env vars or config.
DB_SECRET_ARN = os.getenv("DB_SECRET_ARN") or None

REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TLS = os.getenv("REDIS_TLS", "false").lower() == "true"
REDIS_AUTH_TOKEN = os.getenv("REDIS_AUTH_TOKEN") or None

S3_BUCKET = os.getenv("S3_BUCKET")
# Secret read by /secret and the status page; defaults to the DB secret.
DEMO_SECRET_ARN = os.getenv("DEMO_SECRET_ARN") or DB_SECRET_ARN

# Set to "true" behind an HTTPS ALB so the session cookie is only sent over TLS.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))

# Optional endpoint override for local testing against LocalStack. In real AWS /
# Fargate this is unset, so boto3 uses the task role and the real AWS endpoints.
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL") or None

_boto_common = {"region_name": AWS_REGION}
if AWS_ENDPOINT_URL:
    _boto_common["endpoint_url"] = AWS_ENDPOINT_URL

# boto3 clients use the task role automatically (no keys). Path-style S3
# addressing works against both LocalStack and real S3.
s3 = boto3.client("s3", config=Config(s3={"addressing_style": "path"}), **_boto_common)
sm = boto3.client("secretsmanager", **_boto_common)

_redis_pool = redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_AUTH_TOKEN,
    connection_class=redis.SSLConnection if REDIS_TLS else redis.Connection,
    socket_connect_timeout=5,
    decode_responses=True,
)


_db_credentials = None


def db_credentials(refresh: bool = False) -> dict:
    """Username/password for Postgres, from Secrets Manager when DB_SECRET_ARN is set."""
    global _db_credentials
    if not DB_SECRET_ARN:
        return {"username": DB_USER, "password": DB_PASSWORD}
    if _db_credentials is None or refresh:
        secret = json.loads(sm.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"])
        _db_credentials = {
            "username": secret.get("username", DB_USER),
            "password": secret["password"],
        }
        log.info("loaded database credentials from Secrets Manager (%s)", DB_SECRET_ARN)
    return _db_credentials


def _connect(creds: dict):
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=creds["username"],
        password=creds["password"],
        connect_timeout=5,
    )


def db_conn():
    try:
        return _connect(db_credentials())
    except psycopg2.OperationalError as e:
        # The password may have been rotated since we cached it: re-read the
        # secret once and retry before giving up.
        if DB_SECRET_ARN and "password authentication failed" in str(e):
            log.warning("db auth failed; re-reading credentials from Secrets Manager")
            return _connect(db_credentials(refresh=True))
        raise


def redis_client():
    return redis.Redis(connection_pool=_redis_pool)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Schema -- created lazily so the container still starts (and /health still
# passes) while the database is coming up.
# --------------------------------------------------------------------------
_schema_ready = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS photos (
    id           text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    s3_key       text NOT NULL,
    filename     text NOT NULL,
    content_type text NOT NULL,
    size_bytes   bigint NOT NULL,
    caption      text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS photos_user_created_idx ON photos (user_id, created_at DESC);
"""


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True
    log.info("database schema ready")
