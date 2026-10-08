"""
Binbon -- a small photo-sharing app used to exercise the AWS infrastructure.

    app/web.py    -> HTML UI: sign up, log in, upload photos to S3, status page
    app/api.py    -> JSON infra-check endpoints (/health, /db, /cache, /s3, /secret, ...)
    app/auth.py   -> users (PostgreSQL) and sessions (Redis)
    app/config.py -> env-var configuration and AWS / DB / Redis clients

All configuration comes from environment variables so this drops straight into
an ECS Fargate task definition. AWS calls use boto3, which picks up the task
role's temporary credentials automatically -- there are no access keys here.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, web
from .config import log

app = FastAPI(title="Binbon", version="2.0.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(api.router)
app.include_router(web.router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    if "text/html" in request.headers.get("accept", ""):
        return web.templates.TemplateResponse(
            request, "error.html", {"user": None, "error": str(exc)}, status_code=500
        )
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
