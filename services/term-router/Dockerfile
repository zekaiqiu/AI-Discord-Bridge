# term-router — bridges xterm.js to per-user docker exec sessions.
#
# Trust posture: this container mounts /var/run/docker.sock. Compromise of
# this process is equivalent to host root. Same trust model as
# chat-host-shell. Group membership for the docker socket is the operator's
# concern (set via docker-compose `group_add` or by aligning gids).

FROM python:3.12-slim

# Non-root user (uid 1000) — uvicorn runs as `term`. Docker socket access
# is granted via group membership at deploy time, not by running as root.
RUN groupadd --gid 1000 term \
 && useradd  --uid 1000 --gid 1000 --shell /usr/sbin/nologin --create-home term

WORKDIR /app

# Layer the dep install so code edits don't bust the pip cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# At runtime the chat package is mounted/copied alongside this code; the
# operator wires that in docker-compose so we keep this Dockerfile minimal.
COPY app.py pty_bridge.py file_ops.py ./
COPY static/ ./static/

USER term:term

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
