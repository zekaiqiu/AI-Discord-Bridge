import { useEffect, useImperativeHandle, useRef, forwardRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";

// Browser xterm.js bound to the dev-wizerith /api/terminal WebSocket
// (which docker-execs `bash -l` inside the user's per-user container).
//
// Frame contract is the same one term.wizerith.ai uses — see
// services/dev/pty_bridge.py and the server-side endpoint in
// services/dev/app.py:terminal_ws. Resize envelopes and ping keepalives
// are JSON text frames; everything else is raw stdin.

type Props = {
  // The visible flag drives FitAddon: an xterm in a hidden flex pane
  // reports 0×0 dimensions and the shell ends up with COLS=1 LINES=1,
  // which mangles every prompt. We mount the terminal once on first
  // becoming visible and call fit() on every visible→true transition.
  visible: boolean;
  theme: "dark" | "light";
};

export type TerminalHandle = {
  fit: () => void;
};

export const Terminal = forwardRef<TerminalHandle, Props>(function Terminal(
  { visible, theme },
  ref,
) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XTerm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const mountedRef = useRef(false);
  const pingTimerRef = useRef<number | null>(null);

  // Theme tokens that mirror the Apple-flat palette used elsewhere in dev.
  // Kept as a function so dark/light flip without remounting the terminal.
  const xtermTheme = (t: "dark" | "light") =>
    t === "dark"
      ? {
          background: "#000000",
          foreground: "#f5f5f7",
          cursor: "#0a84ff",
          cursorAccent: "#000000",
          selectionBackground: "rgba(10,132,255,0.35)",
          black: "#000000",
          red: "#ff6b6b",
          green: "#30d158",
          yellow: "#ffd60a",
          blue: "#0a84ff",
          magenta: "#bf5af2",
          cyan: "#64d2ff",
          white: "#f5f5f7",
          brightBlack: "#48484a",
          brightRed: "#ff453a",
          brightGreen: "#34c759",
          brightYellow: "#ffd60a",
          brightBlue: "#409cff",
          brightMagenta: "#bf5af2",
          brightCyan: "#64d2ff",
          brightWhite: "#ffffff",
        }
      : {
          // The user said light-mode terminals are universally unreadable,
          // so we always keep the xterm dark. Same call term.wizerith.ai
          // makes. Kept as a branch so a future "light terminal" toggle is
          // a one-line theme swap.
          background: "#000000",
          foreground: "#f5f5f7",
          cursor: "#0a84ff",
          cursorAccent: "#000000",
          selectionBackground: "rgba(10,132,255,0.35)",
        };

  // Mount-once: first time we become visible, instantiate xterm + connect.
  useEffect(() => {
    if (!visible || mountedRef.current || !hostRef.current) return;
    mountedRef.current = true;

    const term = new XTerm({
      fontFamily:
        "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      fontSize: 13,
      cursorBlink: true,
      scrollback: 5000,
      allowProposedApi: true,
      theme: xtermTheme(theme),
    });
    const fit = new FitAddon();
    const links = new WebLinksAddon();
    term.loadAddon(fit);
    term.loadAddon(links);
    term.open(hostRef.current);
    try { fit.fit(); } catch { /* ignore — happens if host has no size yet */ }
    termRef.current = term;
    fitRef.current = fit;

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/api/terminal`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      const { cols, rows } = term;
      try {
        ws.send(JSON.stringify({ type: "resize", cols, rows }));
      } catch { /* ignore — close raced open */ }
      // Keepalive every 20 s: tells cloudflared / CF edge / Caddy that
      // the WebSocket is still in use, otherwise idle sessions get
      // reaped after ~100 s by intermediaries.
      pingTimerRef.current = window.setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify({ type: "ping" })); } catch { /* ignore */ }
        }
      }, 20000);
      term.focus();
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        term.write(e.data);
      } else {
        // ArrayBuffer — preferred path, server always sends binary.
        const decoder = new TextDecoder();
        term.write(decoder.decode(new Uint8Array(e.data as ArrayBuffer)));
      }
    };
    ws.onclose = () => {
      term.writeln("\r\n\x1b[2m[terminal disconnected — refresh to reconnect]\x1b[0m");
    };
    ws.onerror = () => {
      term.writeln("\r\n\x1b[31m[terminal websocket error]\x1b[0m");
    };

    // Keystrokes → stdin. xterm.js batches keystrokes into onData calls.
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    });
    // Resize envelopes follow Monaco-side layout changes via the
    // imperative handle below; xterm's own resize event fires after
    // FitAddon recomputes.
    term.onResize(({ cols, rows }) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      try { ws.send(JSON.stringify({ type: "resize", cols, rows })); } catch { /* ignore */ }
    });

    // ResizeObserver on the host element so dragging the layout / window
    // refits the terminal without a manual call. The handle's fit() is
    // an extra escape hatch for tab-switching.
    const ro = new ResizeObserver(() => {
      try { fit.fit(); } catch { /* ignore */ }
    });
    ro.observe(hostRef.current);

    return () => {
      ro.disconnect();
      if (pingTimerRef.current !== null) {
        window.clearInterval(pingTimerRef.current);
        pingTimerRef.current = null;
      }
      try { ws.close(); } catch { /* ignore */ }
      try { term.dispose(); } catch { /* ignore */ }
      termRef.current = null;
      fitRef.current = null;
      wsRef.current = null;
      mountedRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  // Re-fit when we become visible — switching from the Output tab to the
  // Terminal tab can change the host's effective size.
  useEffect(() => {
    if (!visible || !fitRef.current) return;
    const t = window.setTimeout(() => {
      try { fitRef.current?.fit(); } catch { /* ignore */ }
      termRef.current?.focus();
    }, 0);
    return () => window.clearTimeout(t);
  }, [visible]);

  // Theme flip from outside — apply to live xterm without remounting.
  useEffect(() => {
    if (!termRef.current) return;
    termRef.current.options.theme = xtermTheme(theme);
  }, [theme]);

  useImperativeHandle(ref, () => ({
    fit: () => {
      try { fitRef.current?.fit(); } catch { /* ignore */ }
    },
  }), []);

  return <div className="xterm-host" ref={hostRef} />;
});
