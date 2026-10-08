"""
Web UI: landing page, sign up / log in, photo dashboard and a service status page.

    Postgres  -> users + photo metadata
    Redis     -> sessions + upload counters
    S3        -> the photo files themselves (bucket stays private; the app streams them)
    Secrets   -> read on the status page to prove the task role can reach it
"""

import re
import time
import uuid
from urllib.parse import quote_plus
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import auth
from .config import (
    COOKIE_SECURE, DEMO_SECRET_ARN, MAX_UPLOAD_MB, S3_BUCKET, SESSION_TTL_SECONDS,
    db_conn, ensure_schema, log, redis_client, s3, sm,
)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_FILES_PER_UPLOAD = 10

# Detect the real image type from the file's first bytes rather than trusting
# the browser-supplied content type or extension.
IMAGE_SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
]


def sniff_image(data: bytes):
    for magic, ctype, ext in IMAGE_SIGNATURES:
        if data.startswith(magic):
            return ctype, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def render(request: Request, name: str, status_code: int = 200, **ctx):
    if "user" not in ctx:
        ctx["user"] = auth.current_user(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def upload_error(message: str):
    return redirect(f"/dashboard?error={quote_plus(message)}")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


templates.env.filters["human_size"] = human_size


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    try:
        total_uploads = int(redis_client().get("stats:uploads") or 0)
    except Exception:
        total_uploads = None
    return render(request, "index.html", total_uploads=total_uploads)


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if auth.current_user(request):
        return redirect("/dashboard")
    return render(request, "signup.html", form={})


@router.post("/signup", response_class=HTMLResponse)
def signup(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
):
    name, email = name.strip(), email.strip().lower()
    form = {"name": name, "email": email}

    error = None
    if not name or len(name) > 80:
        error = "Please enter your name (up to 80 characters)."
    elif not EMAIL_RE.match(email):
        error = "Please enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm_password:
        error = "Passwords do not match."
    if error:
        return render(request, "signup.html", 400, error=error, form=form)

    try:
        user = auth.create_user(name, email, password)
    except auth.EmailTaken:
        return render(
            request, "signup.html", 400,
            error="An account with this email already exists.", form=form,
        )
    log.info("new user signed up: %s", user["id"])
    return _login_response(user, "/dashboard?welcome=1")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if auth.current_user(request):
        return redirect("/dashboard")
    return render(request, "login.html", form={})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(""), password: str = Form("")):
    email = email.strip().lower()
    user = auth.authenticate(email, password)
    if not user:
        return render(
            request, "login.html", 401,
            error="Incorrect email or password.", form={"email": email},
        )
    return _login_response(user, "/dashboard")


@router.post("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = redirect("/")
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


def _login_response(user: dict, next_url: str):
    resp = redirect(next_url)
    resp.set_cookie(
        auth.SESSION_COOKIE,
        auth.start_session(user),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")

    ensure_schema()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, filename, size_bytes, caption, created_at
                FROM photos WHERE user_id = %s ORDER BY created_at DESC;
                """,
                (user["id"],),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    photos = [
        {"id": r[0], "filename": r[1], "size": r[2], "caption": r[3], "created_at": r[4]}
        for r in rows
    ]
    params = request.query_params
    return render(
        request, "dashboard.html",
        user=user,
        photos=photos,
        total_size=sum(p["size"] for p in photos),
        uploaded=int(params.get("uploaded", 0) or 0),
        deleted=params.get("deleted") == "1",
        welcome=params.get("welcome") == "1",
        error=params.get("error"),
        max_mb=MAX_UPLOAD_MB,
    )


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------
@router.post("/photos")
async def upload_photos(
    request: Request,
    photos: list[UploadFile] = File(...),
    caption: str = Form(""),
):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")

    files = [f for f in photos if f.filename]
    if not files:
        return upload_error("Please choose at least one photo.")
    if len(files) > MAX_FILES_PER_UPLOAD:
        return upload_error(f"You can upload up to {MAX_FILES_PER_UPLOAD} photos at once.")

    # Validate everything first so a bad file doesn't leave a half-finished upload.
    validated = []
    for f in files:
        data = await f.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return upload_error(f"{f.filename} is larger than {MAX_UPLOAD_MB} MB.")
        kind = sniff_image(data)
        if not kind:
            return upload_error(f"{f.filename} is not a JPEG, PNG, GIF or WebP image.")
        validated.append((f.filename[:200], data, *kind))

    ensure_schema()
    caption = caption.strip()[:300] or None
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            for filename, data, ctype, ext in validated:
                photo_id = str(uuid.uuid4())
                key = f"photos/{user['id']}/{photo_id}.{ext}"
                s3.put_object(
                    Bucket=S3_BUCKET, Key=key, Body=data, ContentType=ctype,
                    Metadata={"user-id": user["id"]},
                )
                cur.execute(
                    """
                    INSERT INTO photos (id, user_id, s3_key, filename, content_type, size_bytes, caption)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (photo_id, user["id"], key, filename, ctype, len(data), caption),
                )
        conn.commit()
    finally:
        conn.close()

    try:
        redis_client().incrby("stats:uploads", len(validated))
    except Exception:
        log.warning("could not update upload counter", exc_info=True)

    log.info("user %s uploaded %d photo(s)", user["id"], len(validated))
    return redirect(f"/dashboard?uploaded={len(validated)}")


def _owned_photo(user: dict, photo_id: str):
    ensure_schema()
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s3_key, content_type FROM photos WHERE id = %s AND user_id = %s;",
                (photo_id, user["id"]),
            )
            return cur.fetchone()
    finally:
        conn.close()


@router.get("/photos/{photo_id}")
def get_photo(request: Request, photo_id: str):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    row = _owned_photo(user, photo_id)
    if not row:
        return HTMLResponse("Not found", status_code=404)
    obj = s3.get_object(Bucket=S3_BUCKET, Key=row[0])
    return StreamingResponse(
        obj["Body"].iter_chunks(64 * 1024),
        media_type=row[1],
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/photos/{photo_id}/delete")
def delete_photo(request: Request, photo_id: str):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    row = _owned_photo(user, photo_id)
    if row:
        s3.delete_object(Bucket=S3_BUCKET, Key=row[0])
        conn = db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM photos WHERE id = %s;", (photo_id,))
            conn.commit()
        finally:
            conn.close()
    return redirect("/dashboard?deleted=1")


# --------------------------------------------------------------------------
# Status page -- one live check per AWS service
# --------------------------------------------------------------------------
def _check(name: str, target: str, fn):
    start = time.perf_counter()
    try:
        detail = fn()
        ok = True
    except Exception as e:
        detail = str(e).splitlines()[0][:200]
        ok = False
    return {
        "name": name, "target": target or "not configured", "ok": ok,
        "detail": detail, "ms": round((time.perf_counter() - start) * 1000),
    }


def _check_db():
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version;")
            return f"PostgreSQL {cur.fetchone()[0]}"
    finally:
        conn.close()


def _check_redis():
    info = redis_client().info("server")
    return f"Redis {info.get('redis_version', '?')}"


def _check_s3():
    s3.head_bucket(Bucket=S3_BUCKET)
    return "Bucket reachable"


def _check_secret():
    if not DEMO_SECRET_ARN:
        raise RuntimeError("DEMO_SECRET_ARN not set")
    sm.get_secret_value(SecretId=DEMO_SECRET_ARN)
    return "Secret readable (value not shown)"


@router.get("/status", response_class=HTMLResponse)
def status(request: Request):
    from .config import DB_HOST, REDIS_HOST

    checks = [
        _check("Database", f"Aurora / PostgreSQL · {DB_HOST}", _check_db),
        _check("Cache", f"ElastiCache / Redis · {REDIS_HOST}", _check_redis),
        _check("Object storage", f"S3 · {S3_BUCKET}", _check_s3),
        _check("Secrets", f"Secrets Manager · {DEMO_SECRET_ARN}", _check_secret),
    ]
    try:
        user = auth.current_user(request)
    except Exception:
        user = None
    return render(request, "status.html", user=user, checks=checks,
                  all_ok=all(c["ok"] for c in checks))
