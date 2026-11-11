# easychat API image. Targets:
#   runtime (default) — what Cloud Run runs:   docker build -t easychat-api api/
#   test              — runtime + pytest:       docker build --target test api/
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

FROM base AS test
RUN pip install -r requirements-dev.txt
USER app
CMD ["pytest", "-q"]

FROM base AS runtime
USER app
ENV PORT=8080
EXPOSE 8080
# No --proxy-headers: client IPs come from TRUSTED_PROXY_HOPS (see main.client_ip).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"$PORT\" --no-server-header"]
