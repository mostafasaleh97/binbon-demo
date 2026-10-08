# ---------- build stage ----------
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
# psycopg2-binary bundles libpq, so no system build deps are needed
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- runtime stage ----------
FROM python:3.12-slim
WORKDIR /app

# run as a non-root user
RUN useradd --create-home --uid 10001 appuser

# bring in installed packages + console scripts (uvicorn) from the build stage
COPY --from=build /install /usr/local
COPY app/ ./app/

USER appuser
EXPOSE 8000

# container-level healthcheck (the ALB also health-checks /health independently)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]