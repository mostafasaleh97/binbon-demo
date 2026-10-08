"""
Accounts and sessions.

Users live in PostgreSQL; sessions live in Redis/ElastiCache (so every Fargate
task shares them and the service can scale horizontally without sticky sessions).
"""

import hashlib
import hmac
import json
import secrets
import uuid

from fastapi import Request

from .config import SESSION_TTL_SECONDS, db_conn, ensure_schema, redis_client

SESSION_COOKIE = "binbon_session"
_PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


class EmailTaken(Exception):
    pass


def create_user(name: str, email: str, password: str) -> dict:
    ensure_schema()
    user = {"id": str(uuid.uuid4()), "name": name, "email": email}
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE email = %s;", (email,))
            if cur.fetchone():
                raise EmailTaken(email)
            cur.execute(
                "INSERT INTO users (id, name, email, password_hash) VALUES (%s, %s, %s, %s);",
                (user["id"], name, email, hash_password(password)),
            )
        conn.commit()
    finally:
        conn.close()
    return user


def authenticate(email: str, password: str) -> dict | None:
    ensure_schema()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, password_hash FROM users WHERE email = %s;",
                (email,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not verify_password(password, row[3]):
        return None
    return {"id": row[0], "name": row[1], "email": row[2]}


def start_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    redis_client().setex(f"session:{token}", SESSION_TTL_SECONDS, json.dumps(user))
    return token


def end_session(token: str | None):
    if token:
        redis_client().delete(f"session:{token}")


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    raw = redis_client().get(f"session:{token}")
    return json.loads(raw) if raw else None
