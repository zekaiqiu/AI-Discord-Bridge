/* term — xterm.js client (right pane).
 *
 * Three behaviors that aren't obvious from the bare WebSocket wiring:
 *
 *   1. Keepalive: every PING_INTERVAL we send {"type":"ping"} so any
 *      proxy hop (cloudflared, CF edge, Caddy, uvicorn) sees traffic
 *      and doesn't tear the connection down on idle. The server-side
 *      pty_bridge silently drops these, same way it handles resize
 *      envelopes.
 *
 *   2. Auto-reconnect: when the WebSocket closes for any reason, we
 *      reconnect with exponential backoff (1s → 30s cap) and create a
 *      fresh shell. This keeps the page usable across transient drops
 *      and (per user request) a 30-minute idle window. The user loses
 *      shell scrollback / cwd on reconnect — accept that for v1; if it
 *      becomes annoying we add tmux server-side later.
 *
 *   3. Auto-fit: on layout/resize, recompute cols×rows from the pane's
 *      pixel size. Splitter drags trigger this through a ResizeObserver.
 */
(function () {
  "use strict";

  var PING_INTERVAL_MS = 20 * 1000;
  var BACKOFF_INITIAL_MS = 1000;
  var BACKOFF_MAX_MS = 30 * 1000;

  // Theme toggle. The inline <script> in index.html already applied the
  // persisted theme before paint to avoid flash. Here we wire the click +
  // mirror the choice into the xterm theme too.
  (function setupTheme() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    function current() {
      return document.documentElement.getAttribute("data-theme") || "dark";
    }
    function applyIcon() {
      btn.textContent = current() === "dark" ? "☾" : "☀";
    }
    applyIcon();
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("termTheme", next); } catch (_) {}
      applyIcon();
    });
  })();

  // Workspace toggle (Phase: shared-workspace).
  //
  // The cookie name MUST match _WORKSPACE_COOKIE_NAME in
  // services/term-router/app.py. The /api/files/* endpoints also read
  // it, so a single cookie write covers WS + file ops + uploads.
  // Shared workspace UX:
  //   * GET /api/me to learn whether the tenant has it enabled.
  //   * Show the topbar toggle iff enabled.
  //   * Toggle click: write cookie + reconnect WS. The new shell lands
  //     in the chosen container; existing terminal scrollback is left
  //     in place but a banner notes the change.
  var WORKSPACE_COOKIE = "chat_workspace";
  var WORKSPACE_COOKIE_LEGACY = "wizerith_workspace";

  function readWorkspace() {
    var match = document.cookie.match(
      new RegExp("(?:^|; )" + WORKSPACE_COOKIE + "=([^;]+)")
    );
    if (!match) {
      // Fall back to the legacy cookie so existing sessions don't
      // lose their workspace mid-flight. Safe to remove after a
      // couple of months once browsers have rolled over.
      match = document.cookie.match(
        new RegExp("(?:^|; )" + WORKSPACE_COOKIE_LEGACY + "=([^;]+)")
      );
    }
    if (match && (match[1] === "shared" || match[1] === "personal")) {
      return match[1];
    }
    return "personal";
  }

  function writeWorkspace(value) {
    if (value !== "shared" && value !== "personal") return;
    // 30-day cookie. SameSite=Lax matches the CF Access cookie so
    // first-party loads from the same eTLD+1 keep both visible.
    var maxAge = 30 * 24 * 60 * 60;
    var secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie =
      WORKSPACE_COOKIE + "=" + encodeURIComponent(value) +
      "; Path=/; Max-Age=" + maxAge + "; SameSite=Lax" + secure;
    // One-shot cleanup of the legacy cookie so DevTools stays clean.
    document.cookie =
      WORKSPACE_COOKIE_LEGACY + "=; Path=/; Max-Age=0; SameSite=Lax" + secure;
  }

  var workspaceToggle = document.getElementById("workspace-toggle");
  var workspaceButtons = workspaceToggle
    ? workspaceToggle.querySelectorAll(".workspace-opt")
    : [];
  var currentWorkspace = readWorkspace();

  function updateWorkspaceUi() {
    workspaceButtons.forEach(function (btn) {
      var isActive = btn.getAttribute("data-workspace") === currentWorkspace;
      btn.setAttribute("aria-checked", isActive ? "true" : "false");
    });
  }
  updateWorkspaceUi();

  // Probe /api/me: only reveal the toggle when the tenant has
  // shared_workspace_enabled=true. Don't gate the rest of the boot on
  // this — terminal connect can race ahead and the toggle just won't
  // appear if the probe fails.
  fetch("/api/me", { credentials: "same-origin", cache: "no-store" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (me) {
      if (me && me.shared_workspace_enabled && workspaceToggle) {
        workspaceToggle.hidden = false;
      }
    })
    .catch(function () { /* probe-failure is non-fatal */ });

  // The click handler is wired up later (after term + ws + connect are
  // declared), since it needs to write to the terminal and reconnect.
  // See "wire workspace toggle clicks" below.

  var statusEl = document.getElementById("status");
  var connEl = document.getElementById("conn");

  function showStatus(msg) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.hidden = false;
  }
  function hideStatus() {
    if (!statusEl) return;
    statusEl.hidden = true;
  }
  function setConn(state, label) {
    if (!connEl) return;
    connEl.classList.remove("ok", "bad", "reconnecting");
    if (state) connEl.classList.add(state);
    connEl.textContent = label;
  }

  var Terminal = window.Terminal;
  if (!Terminal) {
    showStatus("xterm.js failed to load");
    return;
  }

  var term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    theme: {
      background: "#000000",
      foreground: "#f4f4f5",
      cursor: "#f4f4f5",
      selectionBackground: "#3f3f46",
    },
    scrollback: 5000,
  });
  var termContainer = document.getElementById("terminal");
  term.open(termContainer);
  term.focus();

  // --- Copy / paste keybindings -------------------------------------
  //
  // xterm.js's defaults send literal ^C / ^V to the shell regardless of
  // selection state. Browser users expect Ctrl+C to copy when something
  // is selected, Ctrl+V to paste — same UX as Windows Terminal / the
  // VS Code integrated terminal. Cmd is honored as a Mac alias.
  //
  // Returning false suppresses xterm's default handling for that key;
  // returning true lets it through (so Ctrl+C with no selection still
  // sends SIGINT, which is the load-bearing terminal behavior).
  term.attachCustomKeyEventHandler(function (ev) {
    if (ev.type !== "keydown") return true;
    // Shift-modified variants stay as-is — Ctrl+Shift+C / Cmd+Shift+V
    // remain available for sending raw ^C / ^V if anyone really wants
    // to. (We don't actively map them; xterm's defaults handle Shift+
    // Ctrl as a separate combo on most layouts.)
    if (ev.shiftKey) return true;
    if (!(ev.ctrlKey || ev.metaKey)) return true;

    var key = (ev.key || "").toLowerCase();

    if (key === "c") {
      if (term.hasSelection && term.hasSelection()) {
        var selection = term.getSelection ? term.getSelection() : "";
        if (selection && navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(selection).catch(function () { /* permission denied → fall through silently */ });
        }
        // Clear selection so a quick second Ctrl+C goes to SIGINT —
        // matches Windows Terminal: copy first, interrupt second.
        if (term.clearSelection) term.clearSelection();
        return false;
      }
      // No selection: let xterm send the literal ^C (SIGINT).
      return true;
    }

    if (key === "v") {
      if (navigator.clipboard && navigator.clipboard.readText) {
        navigator.clipboard.readText().then(function (text) {
          if (!text) return;
          // Use term.paste when available — it respects bracketed-paste
          // mode if the shell enabled it (vim, bash with set -o vi,
          // etc.). Falls back to a direct WS send for older xterm.js
          // builds where paste() isn't on the public API.
          if (typeof term.paste === "function") {
            term.paste(text);
          } else if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(text);
          }
        }).catch(function () { /* clipboard read denied → no-op */ });
      }
      return false;
    }

    return true;
  });

  // Fit-on-resize. xterm@v5 lets us read .actualCellWidth/Height after
  // a render via ._core; v3 doesn't. Falls back to a char-cell heuristic.
  function fitTerm() {
    if (!termContainer) return;
    var core = term._core || {};
    var renderer = core._renderService || {};
    var dims = renderer.dimensions || {};
    var cellW = (dims.actualCellWidth || (dims.css && dims.css.cell && dims.css.cell.width)) || 8.4;
    var cellH = (dims.actualCellHeight || (dims.css && dims.css.cell && dims.css.cell.height)) || 17;
    var cols = Math.max(20, Math.floor(termContainer.clientWidth / cellW));
    var rows = Math.max(5, Math.floor(termContainer.clientHeight / cellH));
    try { term.resize(cols, rows); } catch (e) { /* ignore */ }
  }

  requestAnimationFrame(fitTerm);

  var resizeRAF = 0;
  function scheduleFit() {
    if (resizeRAF) return;
    resizeRAF = requestAnimationFrame(function () {
      resizeRAF = 0;
      fitTerm();
    });
  }
  window.addEventListener("resize", scheduleFit);
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(scheduleFit);
    ro.observe(termContainer);
  }

  // --- Connection lifecycle ----------------------------------------

  var ws = null;
  var pingTimer = null;
  var backoff = BACKOFF_INITIAL_MS;
  var manualClose = false;
  var dataListenerInstalled = false;
  var resizeListenerInstalled = false;

  function connect() {
    var wsScheme = location.protocol === "https:" ? "wss:" : "ws:";
    // Pin the requested workspace into the WS URL so the handshake
    // resolves the right container even if the cookie hasn't yet
    // round-tripped (browsers occasionally race cookie writes against
    // immediately-following XHR/WS handshakes).
    var url = wsScheme + "//" + location.host + "/ws"
      + "?workspace=" + encodeURIComponent(currentWorkspace);
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    setConn(null, "connecting…");

    ws.addEventListener("open", function () {
      setConn("ok", "connected");
      hideStatus();
      backoff = BACKOFF_INITIAL_MS;  // reset after a successful open
      // Send initial resize so the server PTY matches the viewport.
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      // Start keepalive. We don't expect a response — sending alone is
      // enough to keep idle proxies from tearing the WebSocket down.
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) { /* ignore */ }
        }
      }, PING_INTERVAL_MS);
    });

    ws.addEventListener("message", function (ev) {
      if (typeof ev.data === "string") {
        term.write(ev.data);
      } else if (ev.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(ev.data));
      } else if (ev.data && typeof ev.data.arrayBuffer === "function") {
        ev.data.arrayBuffer().then(function (buf) {
          term.write(new Uint8Array(buf));
        });
      }
    });

    ws.addEventListener("close", function (ev) {
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      if (manualClose) return;
      // Surface the drop, then schedule a reconnect. Keep the existing
      // terminal contents on screen — only the cursor session is gone.
      setConn("reconnecting", "reconnecting…");
      term.write("\r\n\x1b[33m[disconnected — code " + ev.code + ", reconnecting in "
        + Math.round(backoff / 1000) + "s]\x1b[0m\r\n");
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
    });

    ws.addEventListener("error", function () {
      // Errors fire alongside a close; the close handler does the real
      // reconnect work. Just hint at the cause in the badge.
      setConn("bad", "error");
    });
  }

  // Wire the terminal → ws side ONCE. The xterm callbacks survive
  // reconnects because they read `ws` from the closure each invocation.
  if (!dataListenerInstalled) {
    term.onData(function (data) {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
    });
    dataListenerInstalled = true;
  }
  if (!resizeListenerInstalled) {
    term.onResize(function (size) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "resize", cols: size.cols, rows: size.rows }));
      }
    });
    resizeListenerInstalled = true;
  }

  // Best-effort: close the WS cleanly on tab unload so the server-side
  // exec tears down promptly.
  window.addEventListener("beforeunload", function () {
    manualClose = true;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.close(1000, "page unload"); } catch (e) { /* ignore */ }
    }
  });

  // Wire workspace toggle clicks. Deferred to here because it needs
  // term, ws, manualClose, backoff, and connect to all exist.
  workspaceButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = btn.getAttribute("data-workspace");
      if (!next || next === currentWorkspace) return;
      currentWorkspace = next;
      writeWorkspace(next);
      updateWorkspaceUi();
      term.write(
        "\r\n\x1b[36m[switching to " + next + " workspace…]\x1b[0m\r\n"
      );
      manualClose = true;
      try {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.close(1000, "workspace switch");
        }
      } catch (e) { /* ignore */ }
      // Reset the file pane back to /workspace before the new
      // container's view loads; the old path may not exist in the
      // target container.
      try {
        if (typeof window.__termResetFileList === "function") {
          window.__termResetFileList();
        }
      } catch (e) { /* ignore */ }
      manualClose = false;
      backoff = BACKOFF_INITIAL_MS;
      setTimeout(connect, 0);
    });
  });

  connect();
})();
