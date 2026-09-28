# dev-wizerith — browser Python IDE backend.
#
# Two stages: node:20 builds the Vite SPA, python:3.12-slim runs the FastAPI
# backend. Trust posture mirrors term-router — this container mounts
# /var/run/docker.sock so it can `docker exec` into per-user containers;
# compromise of this process is equivalent to host root.
#
# Frontend assets land at /app/static via COPY --from=frontend.
# Chat package (auth.py + user_container.py + friends) is bind-mounted at
# runtime at /app/services/chat:ro by docker-compose.yml — we don't COPY
# them into the image so the chat code stays a single source of truth.

# ---------------------------------------------------------------------------
# Stage 1: SPA build.
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend

ENV NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

WORKDIR /build/frontend

COPY frontend/package.json ./package.json
COPY frontend/package-lock.jso[n] ./

RUN if [ -f package-lock.json ]; then npm ci; else npm install --no-audit --no-fund; fi

COPY frontend/ ./

# Vite emits to ../static (i.e. /build/static).
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Python runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uid 1000 — uvicorn runs as `dev`. Docker socket access is granted via
# group_add (gid 104) at deploy time, same pattern as term-router.
RUN groupadd --gid 1000 dev \
 && useradd  --uid 1000 --gid 1000 --shell /usr/sbin/nologin --create-home dev

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Backend source — bind-mount path /app/services/chat:ro is wired by
# docker-compose so we import from `services.chat` at runtime, NOT here.
COPY app.py docker_exec.py exec_channel.py file_ops.py lsp_bridge.py projects.py pty_bridge.py state.py /app/

# Built SPA from stage 1.
COPY --from=frontend /build/static /app/static

RUN chown -R 1000:1000 /app

USER 1000:1000

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
