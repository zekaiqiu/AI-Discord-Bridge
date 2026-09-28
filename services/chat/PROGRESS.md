# chat service

A small FastAPI service that fronts a Claude-driven chat experience for the
authenticated user behind Cloudflare Access. Each session is one JSON file on
disk under a per-user directory; messages stream from a `claude` subprocess
over SSE; attachments and a React SPA round out the user-facing surface.

## Manual Felix steps (one-time, post-deploy)

- [ ] **DNS**: confirm `ald3.com` apex record points at the Cloudflare tunnel.
  Cloudflare dashboard → DNS → ald3.com. The record must be proxied (orange
  cloud) and CNAME-flattened or A-record-proxied to the tunnel. If the apex
  currently serves a parked page or redirect, that page will continue to
  show until step 2 is done.

- [ ] **Cloudflare Tunnel public hostname**: Zero Trust → Networks → Tunnels →
  portfolio-tool → Public Hostnames → Add. Hostname `ald3.com`, service
  `http://caddy:80`. (Optionally also add `www.ald3.com` if you want www to
  resolve; otherwise leave it as 404.)

- [ ] **Cloudflare Access**: apply the same Access application/policy that
  protects `dash.ald3.com` to `ald3.com`. Note the application's AUD tag
  (Application → Overview → Application Audience (AUD) Tag). Paste into
  `.env` as `CF_ACCESS_AUD=<aud>`. Set `CF_ACCESS_TEAM=<your-team-subdomain>`
  (the part before `.cloudflareaccess.com` in your team URL).

- [ ] **Pre-create host dirs and deploy**:
  ```
  mkdir -p /home/felix/projects/chat/sessions /home/felix/projects/chat/attachments
  chown -R 1000:1000 /home/felix/projects/chat/sessions /home/felix/projects/chat/attachments
  chmod 0750 /home/felix/projects/chat/sessions /home/felix/projects/chat/attachments
  docker compose up -d --build chat
  docker compose restart caddy
  ```
  NOTE: `/home/felix/projects/chat/` already hosts Ps1 AI's bot code
  (`bot.py`, `public/`). The two new subdirs `sessions/` and `attachments/`
  do not collide with those, but the parent dir now serves two unrelated
  services. If isolation is preferred, Felix may rename the bind-mount
  targets to `/home/felix/projects/chat-web/{sessions,attachments}` and
  update `docker-compose.yml` accordingly — coders did NOT do this rename
  on their own; it's a Felix decision.

## Manual smoke checks (Phase 3 SPA)

Not automated; Felix to verify in browser after deploy:

- [ ] Sidebar lists existing sessions, "+ New chat" creates one, click selects.
- [ ] Hover on a session row reveals the rename (✎) and delete (🗑) buttons.
- [ ] Sidebar **Run** commands (`!tasks`, `!schedules`, `!quota show`,
      `!confirm`, `!agents`, `!new`) send immediately when clicked.
- [ ] Sidebar **Compose** commands pre-fill the textarea and select the first
      `<…>` placeholder so the user can type-replace.
- [ ] Composer auto-grows up to ~40vh; `Enter` sends, `Shift+Enter` newlines.
- [ ] Drag-drop a file onto the composer area: highlight appears, drop creates
      an attachment chip.
- [ ] Stream visibly progresses chunk-by-chunk during a message (i.e. the
      delta events are rendered as they arrive, not all at once on `done`).

## Phase status

- [x] Phase 1 — service skeleton, JWT verification, session CRUD
- [x] Phase 2 — Claude SSE + attachments + background title gen + markers
- [x] Phase 3 — React+Vite SPA, multi-stage Dockerfile, SPA fallback, ≥46 tests
- [x] Phase 4 — compose / Caddyfile / .env wiring (additive overlay)

## Rollback

If anything fails post-deploy:

```bash
docker compose stop chat
# remove the @chat block from infra/caddy/Caddyfile (revert to git HEAD)
git checkout HEAD -- infra/caddy/Caddyfile
docker compose restart caddy
docker compose rm -f chat
```

Pre-deploy snapshot tag: `pre-chat-deploy-<epoch>` (Felix to create with
`git tag pre-chat-deploy-$(date +%s) && git push --tags` before running
`docker compose up`).

## Phase 4 deliverable shape (note for AR2 / Felix)

This pipeline workspace did **not** contain the production
`docker-compose.yml`, `infra/caddy/Caddyfile`, `.env.example`, or project-root
`.gitignore`. Phase 4 therefore created those files in the workspace as the
**additive overlay** Felix merges into the host repo:

* `docker-compose.yml` — minimal verification stack (caddy + chat). The chat
  service block is byte-aligned with the brief; merge the `chat:` block and
  the `chat: { condition: service_started }` member of `caddy.depends_on`
  into the production compose.
* `infra/caddy/Caddyfile` — reproduces the host layout (sourced from the
  reference `/home/felix/projects/portfolio-tool/infra/caddy/Caddyfile`)
  with the new `@chat` block at the same precedence as `@krak` / `@epx`.
  Merge ONLY the `@chat host ald3.com` matcher and its `handle @chat { … }`
  block into the production Caddyfile, BEFORE the `/healthz` dummy and
  BEFORE the default `dashboard:3000` fallback.
* `.env.example` — append the chat section to the production file. (Empty
  RHS for `CF_ACCESS_TEAM` / `CF_ACCESS_AUD` is the brief's expected shape.)
* `.gitignore` — add any missing entries; the existing host file likely
  covers Python/Node detritus already, but the additions for
  `services/chat/static/` and `services/chat/frontend/dist/` are new.

## Known gaps / follow-ups

* **Phase 2.5 — backend-side tool-output markers.** The frontend renders an
  ⚠ BEGIN/END UNTRUSTED USER CONTENT ⚠ fence around assistant content that is
  wrapped by `<<TOOL_OUTPUT>>…<</TOOL_OUTPUT>>` markers, but the Phase 2
  backend does not yet emit those markers (the brief explicitly defers this).
  The fence rendering path is dormant until that change lands.
* **Dev-mode auth bypass.** `npm run dev` proxies `/api` and `/healthz` to a
  local uvicorn that has Cloudflare Access env vars set, which means every
  dev request currently 401s. A dev-only bypass (e.g.
  `CHAT_DEV_BYPASS_EMAIL=…` env read by `auth.require_user`) is out of scope
  for Phase 3 and is intentionally not implemented. Frontend devs working
  without a real Cloudflare tunnel should run uvicorn with `CF_ACCESS_TEAM`
  / `CF_ACCESS_AUD` unset and add the bypass before iterating.

---

## Read this if you are the web agent driving https://ald3.com

You are running inside the `portfolio-chat` container with `/home/felix`
bind-mounted rw and access to the host's docker daemon (via
`/var/run/docker.sock` + `/usr/bin/docker` + group `104`). That means
edits and rebuilds are entirely your responsibility — there is no human
in the loop watching for permission prompts (you run with
`--permission-mode bypassPermissions`).

**Frontend source changes do not take effect until you rebuild.** The
container serves `/app/static/` which was baked into the image during
the last `docker compose build chat`. If you edit
`services/chat/frontend/{src/**,index.html}` and stop there, the user
sees no change. To ship a frontend change end-to-end, run from
`/home/felix/projects/portfolio-tool/`:

```
docker compose build chat && docker compose up -d chat
```

Stage 1 of `services/chat/Dockerfile` runs `npm run build` and writes
to `/build/static`, which stage 2 copies into `/app/static`. The new
container picks up the fresh bundle on `up -d`. Hash-based asset
filenames mean the user's browser pulls the new JS without a cache
flush; the SPA shell (`/`) may need one hard refresh because that's
served fresh per request and the browser may have an in-tab cached
copy from the prior page load.

**Backend Python changes also need a rebuild.** `app.py`, `auth.py`,
`claude_runner.py`, etc. are `COPY`'d into the image at build time —
the same `docker compose build chat && docker compose up -d chat` flow.
Hot-reload is not configured.

**Don't rebuild yourself mid-stream.** If you `docker compose up -d
chat` while a user has an active SSE response in flight, the
container restarts and the user sees a "load failed" disconnect. The
keep-alive frame in `_with_keepalive` only defeats Cloudflare idle
timeout, not your own restart. Schedule rebuilds for moments the user
is between turns, or accept the cosmetic disconnect.

**Things you can also do here**: read/write any file under `/home/felix`
(this includes `.env`, `.claude.json`, project source, the bridge's
code at `~/projects/claude-bridge/`); `docker compose ...` against any
service in the stack (krak, epx, dashboard, portfolio, postgres,
redis, cloudflared, caddy); inspect logs via `docker compose logs
<service> --tail N`. You cannot run `systemctl --user` (the user-bus
auth refuses peer credentials from the container) or `sudo` — for
those, ask the bridge in Discord.

**Hygiene**: never commit `services/chat/static/` (it's gitignored;
it's a build artifact regenerated on every image build). Never commit
or paste contents of `.env`. The bridge's prompt-injection redactor
(see `~/projects/claude-chat/bot.py`) does NOT cover this chat
service; it's redactor-free, so be careful what you echo back to chat
when reading user-submitted files like krak's recruit JSONs.
