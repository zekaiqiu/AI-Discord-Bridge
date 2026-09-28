type LspStatus = "connecting" | "ready" | "closed" | "error";
export type Workspace = "personal" | "shared";

type Props = {
  email: string;
  theme: "dark" | "light";
  onToggleTheme: () => void;
  activePath: string | null;
  dirty: boolean;
  running: boolean;
  lspStatus: LspStatus;
  workspace: Workspace;
  onWorkspaceChange: (w: Workspace) => void;
  onSave: () => void;
  onRun: () => void;
  onKill: () => void;
  onNewTerminal: () => void;
  onResetLayout: () => void;
  children?: React.ReactNode;
};

// Map the current dev.* hostname to its apex chat sibling so each
// tenant jumps back to the right chat instance. Falls back to "/" for
// local dev where the hostname isn't dev.*.
function chatHomeUrl(): string {
  if (typeof window === "undefined") return "/";
  const host = window.location.hostname;
  if (host.startsWith("dev.")) {
    return `${window.location.protocol}//${host.slice(4)}/`;
  }
  return "/";
}

// "Home" link points at the ald3.com services hub. ONLY shown when the
// current hostname is on the ald3.com tenant — on dev.wizerith.ai the
// link must not appear (wizerith stays isolated from any ald3 surface;
// see the wizerith-isolation memory).
function ald3HomeUrl(): string | null {
  if (typeof window === "undefined") return null;
  const host = window.location.hostname;
  const onAld3 = host === "ald3.com" || host.endsWith(".ald3.com");
  return onAld3 ? `${window.location.protocol}//ald3.com/` : null;
}

// Branding label for the topbar — derived from the live host so each
// tenant displays its own hostname rather than a hardcoded name.
function brandLabel(): string {
  if (typeof window === "undefined") return "dev";
  return window.location.host;
}

const LSP_LABEL: Record<LspStatus, string> = {
  connecting: "starting pyright…",
  ready: "pyright ready",
  closed: "pyright disconnected",
  error: "pyright failed (autocomplete/hover unavailable)",
};

export function TopBar({
  email,
  theme,
  onToggleTheme,
  activePath,
  dirty,
  running,
  lspStatus,
  workspace,
  onWorkspaceChange,
  onSave,
  onRun,
  onKill,
  onNewTerminal,
  onResetLayout,
  children,
}: Props) {
  const homeUrl = ald3HomeUrl();
  return (
    <header className="topbar">
      <div className="topbar-left">
        <a
          className="btn btn-icon back-to-chat"
          href={chatHomeUrl()}
          title="Back to chat"
          aria-label="Back to chat"
        >
          ←
        </a>
        {homeUrl && (
          <a
            className="btn btn-secondary home-link"
            href={homeUrl}
            title="Back to ald3.com home"
          >
            Home
          </a>
        )}
        <span className="brand">{brandLabel()}</span>
        <div
          className="workspace-toggle"
          role="radiogroup"
          aria-label="Workspace"
          title={
            workspace === "shared"
              ? "Editing the shared workspace (visible to everyone in the tenant)"
              : "Editing your personal workspace"
          }
        >
          {(["personal", "shared"] as const).map((w) => (
            <button
              key={w}
              type="button"
              role="radio"
              aria-checked={workspace === w}
              className={`workspace-opt ${workspace === w ? "is-active" : ""}`}
              onClick={() => { if (workspace !== w) onWorkspaceChange(w); }}
            >
              {w}
            </button>
          ))}
        </div>
        {activePath && (
          <span className="active-path">
            {activePath}
            {dirty && <span className="dot"> •</span>}
          </span>
        )}
      </div>
      <div className="topbar-right">
        {children}
        <span
          className={`lsp-indicator lsp-${lspStatus}`}
          title={LSP_LABEL[lspStatus]}
        >
          LSP
        </span>
        <button
          className="btn btn-secondary"
          onClick={onSave}
          disabled={!activePath || !dirty}
          title="Save (Cmd/Ctrl+S)"
        >
          Save
        </button>
        {running ? (
          <button
            className="btn btn-danger"
            onClick={onKill}
            title="Stop (Cmd/Ctrl+Enter)"
          >
            ■ Stop
          </button>
        ) : (
          <button
            className="btn btn-primary"
            onClick={onRun}
            disabled={!activePath}
            title="Run (Cmd/Ctrl+Enter)"
          >
            ▶ Run
          </button>
        )}
        <button
          className="btn btn-icon"
          onClick={onNewTerminal}
          title="New terminal (Cmd/Ctrl+`)"
          aria-label="New terminal"
        >
          ▌_
        </button>
        <button
          className="btn btn-icon"
          onClick={onResetLayout}
          title="Reset panel layout to defaults"
          aria-label="Reset layout"
        >
          ⊞
        </button>
        <button
          className="btn btn-icon"
          onClick={onToggleTheme}
          title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
        <span className="email" title={email}>
          {email.split("@")[0]}
        </span>
      </div>
    </header>
  );
}
