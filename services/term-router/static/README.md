# term-router static bundle

## Bundle provenance

This directory contains pre-built, version-pinned copies of xterm.js so the
service has zero external CDN dependencies at runtime — Caddy serves these
files directly.

| File        | Version | Source URL                                                                  | sha256                                                             |
| ----------- | ------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `xterm.js`  | 5.3.0   | https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js                   | `fc1dd31b221e3e5f929486e07a80b477a8aaf9dce2b4f9c3ffe7dd25f370655d` |
| `xterm.css` | 5.3.0   | https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css                  | `64ee6c4db69b4224d3362aced0fd4cdd620e0e60b3d01566450ae2d4b9e81849` |

Verify locally any time with:

```bash
sha256sum services/term-router/static/xterm.js services/term-router/static/xterm.css
```

The bundle is the UMD build (defines `window.Terminal`); `term.js` reads
`window.Terminal` to instantiate the terminal — see the comment at the top
of that file.

## How to upgrade

xterm.js releases are infrequent enough that we treat this as a manual,
auditable step rather than automating it. **Do not** point production at a
CDN — the whole reason these files are checked in is so a future xterm.js
maintainer can't push code into our terminal.

1. Pick the new version from <https://github.com/xtermjs/xterm.js/releases>
   (or the npm `xterm` package).
2. Download the new bundle locally:
   ```bash
   curl -sSL -o /tmp/xterm.js  "https://cdn.jsdelivr.net/npm/xterm@<NEW>/lib/xterm.min.js"
   curl -sSL -o /tmp/xterm.css "https://cdn.jsdelivr.net/npm/xterm@<NEW>/css/xterm.min.css"
   ```
3. Verify the sha256 matches what you expect (sanity check vs. the
   xterm.js GitHub release notes / npm registry):
   ```bash
   sha256sum /tmp/xterm.js /tmp/xterm.css
   ```
4. Replace the files in this directory:
   ```bash
   cp /tmp/xterm.js  services/term-router/static/xterm.js
   cp /tmp/xterm.css services/term-router/static/xterm.css
   ```
5. Update the table in **this README** with the new version and sha256
   values, in the same commit.
6. Smoke test: `docker compose build term-router && docker compose up -d
   term-router`, open `term.ald3.com`, confirm the prompt appears and a
   resize works.

## Why a UMD bundle and not ES modules

UMD lets `term.js` reference `window.Terminal` directly without a build
step. We keep this service's frontend toolchain to "open the file in a
browser" — no bundler, no transpiler, no node_modules at deploy time.
