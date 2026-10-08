"""
Infrastructure check endpoints (JSON). Each one exercises exactly one service:

    GET  /health          -> ALB health check (NO dependencies on purpose)
    GET  /config          -> shows what's wired, without leaking secrets
    GET  /db              -> Aurora PostgreSQL connectivity (SELECT version())
    POST /db              -> Postgres write + read back
    POST /cache           -> ElastiCache Redis/Valkey SET
    GET  /cache/{key}     -> Redis GET
    POST /upload          -> S3 put_object (via task role, no keys)
    GET  /download/{key}  -> S3 get_object
    GET  /s3              -> S3 list_objects_v2
    GET  /secret          -> Secrets Manager get_secret_value (values masked)
"""

import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import (
    AWS_REGION, DB_HOST, DB_NAME, DB_SECRET_ARN, REDIS_HOST, REDIS_TLS, S3_BUCKET, DEMO_SECRET_ARN,
    s3, sm, db_conn, redis_client, utcnow, log,
)

router = APIRouter()


@router.get("/health")
def health():
    # Intentionally dependency-free. If this checked the DB, the ALB would never
    # mark the task healthy while the DB was still coming up, and the task would
    # be killed in a loop. Keep health cheap; test dependencies on their own paths.
    return {"status": "ok"}


@router.get("/api")
def api_index():
    return {
        "service": "binbon",
        "endpoints": [
            "/health", "/config",
            "GET /db", "POST /db",
            "POST /cache", "GET /cache/{key}",
            "POST /upload", "GET /download/{key}", "GET /s3",
            "GET /secret",
        ],
    }


@router.get("/config")
def config():
    # Never prints secret values.
    return {
        "aws_region": AWS_REGION,
        "db_host": DB_HOST,
        "db_name": DB_NAME,
        "redis_host": REDIS_HOST,
        "redis_tls": REDIS_TLS,
        "s3_bucket": S3_BUCKET,
        "db_secret_arn": DB_SECRET_ARN,
        "secret_arn_set": bool(DEMO_SECRET_ARN),
    }


# --------------------------------------------------------------------------
# Aurora PostgreSQL
# --------------------------------------------------------------------------
@router.get("/db")
def db_check():
    try:
        conn = db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                version = cur.fetchone()[0]
        finally:
            conn.close()
        return {"ok": True, "postgres_version": version}
    except Exception as e:
        log.exception("db check failed")
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


class DBItem(BaseModel):
    message: str


@router.post("/db")
def db_write(item: DBItem):
    try:
        row_id = str(uuid.uuid4())
        conn = db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS demo_items (
                        id text PRIMARY KEY,
                        message text,
                        created_at timestamptz DEFAULT now()
                    );
                    """
                )
                cur.execute(
                    "INSERT INTO demo_items (id, message) VALUES (%s, %s);",
                    (row_id, item.message),
                )
                cur.execute("SELECT count(*) FROM demo_items;")
                count = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "inserted_id": row_id, "total_rows": count}
    except Exception as e:
        log.exception("db write failed")
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# --------------------------------------------------------------------------
# ElastiCache (Redis / Valkey)
# --------------------------------------------------------------------------
class CacheItem(BaseModel):
    key: str
    value: str


@router.post("/cache")
def cache_set(item: CacheItem):
    try:
        r = redis_client()
        r.set(item.key, item.value)
        hits = r.incr("demo:hits")
        return {"ok": True, "key": item.key, "demo_hits": hits}
    except Exception as e:
        log.exception("cache set failed")
        raise HTTPException(status_code=500, detail=f"Redis error: {e}")


@router.get("/cache/{key}")
def cache_get(key: str):
    try:
        value = redis_client().get(key)
    except Exception as e:
        log.exception("cache get failed")
        raise HTTPException(status_code=500, detail=f"Redis error: {e}")
    if value is None:
        raise HTTPException(status_code=404, detail=f"key '{key}' not found")
    return {"ok": True, "key": key, "value": value}


# --------------------------------------------------------------------------
# S3
# --------------------------------------------------------------------------
@router.post("/upload")
def s3_upload():
    try:
        key = f"demo/{uuid.uuid4()}.txt"
        body = f"binbon demo object created at {utcnow()}"
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body.encode())
        return {"ok": True, "bucket": S3_BUCKET, "key": key}
    except Exception as e:
        log.exception("s3 upload failed")
        raise HTTPException(status_code=500, detail=f"S3 error: {e}")


@router.get("/download/{key:path}")
def s3_download(key: str):
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return {"ok": True, "key": key, "content": obj["Body"].read().decode()}
    except Exception as e:
        log.exception("s3 download failed")
        raise HTTPException(status_code=500, detail=f"S3 error: {e}")


@router.get("/s3")
def s3_list():
    try:
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="demo/", MaxKeys=20)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        return {"ok": True, "bucket": S3_BUCKET, "count": len(keys), "keys": keys}
    except Exception as e:
        log.exception("s3 list failed")
        raise HTTPException(status_code=500, detail=f"S3 error: {e}")


# --------------------------------------------------------------------------
# Secrets Manager
# --------------------------------------------------------------------------
@router.get("/secret")
def secret_read():
    if not DEMO_SECRET_ARN:
        raise HTTPException(status_code=400, detail="DEMO_SECRET_ARN not set")
    try:
        resp = sm.get_secret_value(SecretId=DEMO_SECRET_ARN)
        raw = resp.get("SecretString", "")
    except Exception as e:
        log.exception("secret read failed")
        raise HTTPException(status_code=500, detail=f"Secrets Manager error: {e}")

    # Prove we could read and parse the secret WITHOUT returning its values.
    try:
        keys = list(json.loads(raw).keys())
    except json.JSONDecodeError:
        keys = ["<plaintext secret>"]
    return {"ok": True, "secret_keys": keys, "note": "values masked on purpose"}
