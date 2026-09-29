# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1: build the SPA. Vite is configured with outDir=../static, so a
# successful ``npm run build`` from /build/frontend lands assets at
# /build/static. We copy package manifests first to maximise layer caching.
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend

ENV NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    # Cap node's heap so a vite build on the RAM-tight prod host fails
    # cleanly instead of swap-thrashing the whole box (which is what
    # wedged it on a previous in-place build).
    NODE_OPTIONS=--max-old-space-size=3072

WORKDIR /build/frontend

COPY frontend/package.json ./package.json
# package-lock.json is optional; if absent we fall back to ``npm install``.
COPY frontend/package-lock.jso[n] ./

RUN if [ -f package-lock.json ]; then npm ci; else npm install --no-audit --no-fund; fi

COPY frontend/ ./

# Vite emits to ../static (i.e. /build/static).
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: python runtime. Same uid/gid pattern as Phase 1; layers ordered
# for caching (requirements before code).
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Match the krak/epx uid/gid pattern so bind-mounted host dirs (e.g. session
# storage) can be chowned to 1000:1000 on the host and remain writable here.
RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid 1000 --home /app --shell /usr/sbin/nologin app

# ---------------------------------------------------------------------------
# Item 1: claude-runner system user (uid/gid 2000).
#
# This user does NOT own anything in the chat image's own runtime — the
# chat service still runs as `app` (uid 1000) per the USER directive
# below. The claude-runner identity is consumed exclusively by per-user
# containers (which reuse this same image): user_container.py issues
# `docker exec --user 2000:2000 -e HOME=/var/claude-runner ...` so the
# claude CLI executes against credentials seeded under
# /var/claude-runner/.claude that uid 1000 (the user's interactive
# /workspace shell) cannot read. /workspace itself is shared between
# uid 1000 and gid 2000 via 2775 setgid in user_container.py.
#
# Directories pre-created here so the per-user provisioning path can
# rely on the layout existing even before any docker exec mkdir runs;
# /var/claude-runner/{projects,sessions} mirror what claude itself
# writes into HOME during a session.
# ---------------------------------------------------------------------------
RUN groupadd -g 2000 claude-runner \
 && useradd -u 2000 -g 2000 -M -s /usr/sbin/nologin claude-runner \
 && mkdir -p /var/claude-runner /var/claude-runner/.claude /var/claude-runner/projects /var/claude-runner/sessions \
 && chown -R 2000:2000 /var/claude-runner \
 && chmod 0700 /var/claude-runner /var/claude-runner/.claude

# The docker CLI binary is bind-mounted in from the host (compose:
# /usr/bin/docker:ro). Its only library dep is libc, which python:3.12-slim
# already has, so no extra packages are needed here. Talking to the host
# daemon requires the docker.sock bind mount + group_add 104 in compose.
#
# procps gives us `ps` / `top` etc. — needed by the chat-host-shell sidecar
# (which reuses this image with pid:host) so admin web sessions can `ps`
# the host process tree the same way the Discord bridge can. ~1.5 MB.
# psmisc adds `pstree`/`killall`. iproute2 gives `ss`/`ip` so the agent
# can introspect the (host-namespaced) network listeners.
#
# The remaining packages are guest-developer tooling consumed inside
# per-user containers (which reuse this image): common build tools,
# editors, search/text utilities, archive handlers, plus the runtime
# deps for rootless podman (uidmap/slirp4netns/fuse-overlayfs).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      procps psmisc iproute2 \
      git curl wget ca-certificates openssh-client \
      build-essential pkg-config \
      ripgrep jq fzf tree less \
      vim nano \
      tmux htop \
      unzip zip xz-utils \
      dnsutils iputils-ping netcat-openbsd \
      ffmpeg \
      podman uidmap slirp4netns fuse-overlayfs \
      libcairo2 libpango-1.0-0 libpangoft2-1.0-0 \
      libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
      fonts-liberation fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Item 2: Nix single-user install. The store at /nix is baked into the
# image and shared across all per-user containers via Docker layer reuse.
# Users can install packages with `nix-env -iA nixpkgs.<pkg>` which
# writes to ~/.nix-profile (= /workspace/.nix-profile at runtime, in their
# persistent volume). The store delta (new entries under /nix/store) lives
# in the container's writable layer — survives stop/start, lost on rm.
#
# We run the installer as the `app` user with HOME=/app so per-user state
# (channels, defexpr, profile symlink) lands under /app/.nix-* rather
# than polluting the build environment. At runtime each user gets their
# own ~/.nix-profile under /workspace; /etc/profile.d/zz-nix.sh below
# falls back to the system default profile when the per-user one is
# absent (i.e. before their first nix-env call).
RUN mkdir -m 0755 /nix && chown 1000:1000 /nix \
 && mkdir -p /app && chown 1000:1000 /app
USER 1000
RUN curl -fsSL https://nixos.org/nix/install -o /tmp/nix-install \
 && USER=app HOME=/app sh /tmp/nix-install --no-daemon --yes --no-channel-add \
 && rm -f /tmp/nix-install
USER root
RUN printf '%s\n' \
      '# Source nix paths for all users. Per-user ~/.nix-profile takes' \
      '# precedence over the system-wide profile baked into the image.' \
      '# /app/.nix-profile is the install seeded at image build (Nix 2.34+' \
      '# uses XDG state paths; /app/.nix-profile is a stable symlink into' \
      '# /app/.local/state/nix/profiles/profile created by the installer).' \
      'if [ -d "$HOME/.nix-profile/bin" ]; then' \
      '    PATH="$HOME/.nix-profile/bin:$PATH"' \
      'fi' \
      'PATH="/app/.nix-profile/bin:$PATH"' \
      'export PATH' \
      'export NIX_PROFILES="/app/.nix-profile $HOME/.nix-profile"' \
    > /etc/profile.d/zz-nix.sh \
 && chmod 0755 /etc/profile.d/zz-nix.sh

# Item 3: rootless podman config. Subuid/subgid mappings give the `app`
# user a 65k-uid range for inner-container user-namespaces; storage is
# under /workspace so user images survive container recreate.
# cgroup_manager=cgroupfs (vs default systemd) since per-user containers
# don't run systemd. The docker→podman alias is interactive-only via
# /etc/profile.d to avoid shadowing /usr/bin/docker (bind-mounted from
# host into the chat service container, where it must remain real docker
# for talking to the host daemon).
RUN echo "app:100000:65536" >> /etc/subuid \
 && echo "app:100000:65536" >> /etc/subgid \
 && mkdir -p /etc/containers \
 && printf '[storage]\ndriver = "fuse-overlayfs"\nrunroot = "/run/user/1000/containers"\ngraphroot = "/workspace/.local/share/containers/storage"\n' \
    > /etc/containers/storage.conf \
 && printf '[engine]\ncgroup_manager = "cgroupfs"\nevents_logger = "file"\n' \
    > /etc/containers/containers.conf \
 && printf '%s\n' '#!/bin/sh' 'alias docker=podman' \
    > /etc/profile.d/zz-podman-alias.sh \
 && chmod 0755 /etc/profile.d/zz-podman-alias.sh

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Quant / data-science stack baked into site-packages so the per-user
# container (which reuses this image) can `import pandas as pd`,
# `import vectorbt as vbt`, etc. without per-session pip installs. Kept
# in its own layer so chat-backend dep bumps don't bust this cache —
# rebuilding numba/llvmlite/cvxpy chains is slow.
COPY requirements-user.txt /app/requirements-user.txt
RUN pip install --no-cache-dir -r /app/requirements-user.txt

# Backend Python source. ``COPY .`` would also pull in ``frontend/`` and
# ``static/`` from the host; we list explicit roots so neither sneaks in.
# Tests and conftest.py are excluded via .dockerignore (run on host, not in
# the production image).
COPY app.py auth.py local_auth.py local_auth_routes.py storage.py attachments.py artifact_runner.py claude_runner.py session_process.py haihub_runner.py local_runner.py tokenhub_runner.py prompt_blocks.py image_gen.py markers.py account_router.py user_container.py user_container_eviction.py run_turn.py sessions.py anthropic_auth_proxy.py chat_scheduler.py token_ledger.py /app/
COPY app.py auth.py local_auth.py local_auth_routes.py storage.py attachments.py artifact_runner.py claude_runner.py session_process.py haihub_runner.py local_runner.py mimo_runner.py kimi_runner.py prompt_blocks.py image_gen.py markers.py account_router.py user_container.py user_container_eviction.py run_turn.py sessions.py anthropic_auth_proxy.py chat_scheduler.py token_ledger.py /app/

# Quant-research CLIs — research-time tools the chat-driven model invokes
# from inside per-user containers (which reuse this image). Both target
# /usr/local/bin/python3 (system 3.12) because it has the full quant
# stack from requirements-user.txt; linuxbrew Python 3.14 lacks numba/
# quantstats. tearsheet wraps quantstats; robustness runs walk-forward +
# stationary-bootstrap MC on a returns series. See claude_runner.py
# system prompt for the model-facing usage notes.
COPY scripts/tearsheet scripts/robustness /usr/local/bin/
RUN chmod 0755 /usr/local/bin/tearsheet /usr/local/bin/robustness

# Built SPA from stage 1.
COPY --from=frontend /build/static /app/static

RUN chown -R 1000:1000 /app

USER 1000

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
