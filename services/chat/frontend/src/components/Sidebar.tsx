import { MouseEvent as ReactMouseEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  MeResponse,
  SearchResult,
  SessionSummary,
  Workspace,
  exportSessionUrl,
} from "../api";
import { TFunc, useT } from "../i18n";
import { ConfirmDialog } from "./ConfirmDialog";

const MAX_VISIBLE_SESSIONS = 50;

// Let cmd/ctrl/shift/middle-click on an <a> fall through to browser default
// (open in new tab / window) instead of triggering SPA navigation.
function isModifierClick(e: ReactMouseEvent): boolean {
  return e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0;
}

export type UiState =
  | "minimized-sidebar"
  | "expanded-sidebar"
  | "dashboard:agents"
  | "dashboard:usage"
  | "dashboard:settings"
  | "dashboard:about";

export type SidebarFilter = "all" | "starred" | "archived";

export interface SidebarProps {
  sessions: SessionSummary[];
  currentSessionId: string | null;
  uiTop: UiState;
  filter: SidebarFilter;
  onFilterChange: (f: SidebarFilter) => void;
  searchQuery: string;
  onSearchQueryChange: (q: string) => void;
  searchResults: SearchResult[];
  searching: boolean;
  onChevron: () => void;
  onNewChat: () => void;
  /** Open an additional chat window (split pane) alongside the current one. */
  onNewChatWindow: () => void;
  /** False when the max number of windows is already open — disables the
   *  New Chat Window button. */
  canOpenWindow: boolean;
  onSelectSession: (id: string) => void;
  /** Open the given session in an additional chat window (split pane). */
  onOpenSessionInNewWindow: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onToggleStar: (id: string, starred: boolean) => void;
  onToggleArchive: (id: string, archived: boolean) => void;
  onSetFolder: (id: string, folder: string | null) => void;
  onOpenAgents: () => void;
  onOpenUsage: () => void;
  onOpenSettings: () => void;
  onOpenAbout: () => void;
  me: MeResponse | null;
  // Default workspace for the next "+ New Chat" — mirrored from
  // localStorage in App.tsx. The toggle in the sidebar controls this.
  // Existing sessions display whichever workspace they were created
  // with (read off SessionSummary.workspace).
  workspace: Workspace;
  onWorkspaceChange: (w: Workspace) => void;
}

export function Sidebar(props: SidebarProps): JSX.Element {
  const {
    sessions,
    currentSessionId,
    uiTop,
    filter,
    onFilterChange,
    searchQuery,
    onSearchQueryChange,
    searchResults,
    searching,
    onChevron,
    onNewChat,
    onNewChatWindow,
    canOpenWindow,
    onSelectSession,
    onOpenSessionInNewWindow,
    onRename,
    onDelete,
    onToggleStar,
    onToggleArchive,
    onSetFolder,
    onOpenAgents,
    onOpenUsage,
    onOpenSettings,
    onOpenAbout,
    me,
    workspace,
    onWorkspaceChange,
  } = props;

  const t = useT();
  // Session pending delete-confirmation (drives the in-app modal instead of
  // the browser's window.confirm).
  const [confirmDelete, setConfirmDelete] = useState<SessionSummary | null>(null);
  const collapsed = uiTop === "minimized-sidebar";
  const isAdmin = me?.role === "admin";
  // Same SPA image serves every tenant; derive the IDE workspace URL
  // from the current host so it scales to N tenants without hostname-
  // specific branches. Mirrors the helper in Artifact.tsx.
  //   apex (example.com)         → dev.example.com
  //   subdomain (chat.foo.com)   → dev.foo.com
  //   2-part fallback (foo.com)  → dev.foo.com
  //
  // Variable name kept as `terminalHref` since the Sidebar button's
  // call-to-action still says "Terminal" — dev.<host> hosts the
  // PyCharm-style workspace that includes a Terminal panel.
  const terminalHref = (() => {
    if (typeof window === "undefined") return "";
    const host = window.location.host;
    const devHost = host.startsWith("chat.")
      ? host.replace(/^chat\./, "dev.")
      : host.split(".").length === 2
      ? "dev." + host
      : "dev." + host.replace(/^[^.]+\./, "");
    // Pass the current chat workspace through as a query param so the dev
    // IDE lands in the matching container (personal vs shared) — the dev
    // backend reads it on first request and stores it in the same cookie
    // the terminal honors.
    const qs = workspace === "shared" ? "?workspace=shared" : "?workspace=personal";
    return `${window.location.protocol}//${devHost}/${qs}`;
  })();

  // Apply the active filter, then group by folder. "Archived" tab shows
  // only archived; "Starred" shows starred (non-archived); "All" shows
  // non-archived.
  const filtered = sessions.filter((s) => {
    if (filter === "archived") return s.archived;
    if (filter === "starred") return s.starred && !s.archived;
    return !s.archived;
  });
  const visible = filtered.slice(0, MAX_VISIBLE_SESSIONS);

  // Group by folder for the "All" tab so per-folder grouping is visible
  // without a separate folder tab. Untagged sessions go into a default
  // "Conversations" group.
  const groups: { folder: string | null; sessions: SessionSummary[] }[] = [];
  if (filter === "all") {
    const map = new Map<string | null, SessionSummary[]>();
    for (const s of visible) {
      const key = s.folder || null;
      const arr = map.get(key) || [];
      arr.push(s);
      map.set(key, arr);
    }
    // Default folder first, then alpha-sorted folders.
    const folders = [...map.keys()].filter((k) => k !== null) as string[];
    folders.sort();
    if (map.has(null)) groups.push({ folder: null, sessions: map.get(null)! });
    for (const f of folders) groups.push({ folder: f, sessions: map.get(f)! });
  } else {
    groups.push({ folder: null, sessions: visible });
  }

  const trimmedQuery = searchQuery.trim();
  const showingSearch = trimmedQuery.length > 0;

  return (
    <aside className={`sidebar ${collapsed ? "is-collapsed" : ""}`}>
      <div className="sidebar-top">
        <button
          type="button"
          className="sidebar-collapse-toggle"
          onClick={onChevron}
          aria-label={collapsed ? t("sidebar.expand_aria") : t("sidebar.collapse_aria")}
          title={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
        >
          {collapsed ? "›" : "‹"}
        </button>
        {!collapsed && me && (
          <div className="sidebar-me" title={me.email}>
            {me.email}
          </div>
        )}
      </div>

      <a
        className="sidebar-new-chat"
        href="?new=1"
        title={t("sidebar.new_chat")}
        onClick={(e) => {
          if (isModifierClick(e)) return;
          e.preventDefault();
          onNewChat();
        }}
      >
        <span className="icon">＋</span>
        {!collapsed && <span className="label">{t("sidebar.new_chat")}</span>}
      </a>

      <button
        type="button"
        className="sidebar-new-window"
        onClick={onNewChatWindow}
        disabled={!canOpenWindow}
        title={
          canOpenWindow ? t("sidebar.new_window_title") : t("sidebar.new_window_max")
        }
        aria-label={t("sidebar.new_window")}
      >
        <span className="icon" aria-hidden="true">⧉</span>
        {!collapsed && <span className="label">{t("sidebar.new_window")}</span>}
      </button>

      {(() => {
        // On wizerith hosts the shared topbar (`/_theme/wizerith-theme.js`)
        // owns the Personal/Shared toggle — the workspace it picks drives
        // chat's session filter via the `wizerith:workspace` CustomEvent
        // wired in App.tsx. Hiding the sidebar copy avoids two competing
        // sources of truth on the same screen.
        const host = typeof window !== "undefined" ? window.location.hostname : "";
        const onWizerith = host === "wizerith.ai" || host.endsWith(".wizerith.ai");
        if (collapsed || !me?.shared_workspace_enabled || onWizerith) return null;
        return (
          <div
            className="sidebar-workspace-toggle"
            role="radiogroup"
            aria-label={t("sidebar.workspace.aria")}
            title={t(
              workspace === "shared"
                ? "sidebar.workspace.title.shared"
                : "sidebar.workspace.title.personal",
            )}
          >
            {(["personal", "shared"] as Workspace[]).map((w) => (
              <button
                key={w}
                type="button"
                role="radio"
                aria-checked={workspace === w}
                className={`sidebar-workspace-opt ${workspace === w ? "is-active" : ""}`}
                onClick={() => onWorkspaceChange(w)}
              >
                {t(`sidebar.workspace.${w}`)}
              </button>
            ))}
          </div>
        );
      })()}

      {!collapsed && (
        <>
          <div className="sidebar-search">
            <input
              type="search"
              className="sidebar-search-input"
              placeholder={t("sidebar.search_placeholder")}
              value={searchQuery}
              onChange={(e) => onSearchQueryChange(e.target.value)}
              aria-label={t("sidebar.search_aria")}
            />
            {showingSearch && (
              <button
                type="button"
                className="sidebar-search-clear"
                onClick={() => onSearchQueryChange("")}
                aria-label={t("sidebar.search_clear_aria")}
                title={t("sidebar.search_clear")}
              >
                ✕
              </button>
            )}
          </div>

          {!showingSearch && (
            <div className="sidebar-tabs" role="tablist">
              {(["all", "starred", "archived"] as SidebarFilter[]).map((f) => (
                <button
                  key={f}
                  type="button"
                  role="tab"
                  aria-selected={filter === f}
                  className={`sidebar-tab ${filter === f ? "is-active" : ""}`}
                  onClick={() => onFilterChange(f)}
                >
                  {t(`sidebar.tab.${f}`)}
                </button>
              ))}
            </div>
          )}

          <div className="sidebar-scroll">
          {showingSearch ? (
            <SidebarSection title={searching ? t("sidebar.searching") : t(searchResults.length === 1 ? "sidebar.matches.one" : "sidebar.matches.other", { count: searchResults.length })}>
              {searchResults.length === 0 && !searching ? (
                <div className="sidebar-empty">{t("sidebar.no_matches")}</div>
              ) : (
                <ul className="session-list">
                  {searchResults.map((s) => (
                    <SessionRow
                      key={s.id}
                      session={s}
                      snippet={s.snippet}
                      active={s.id === currentSessionId}
                      canOpenWindow={canOpenWindow}
                      onSelect={() => onSelectSession(s.id)}
                      onOpenInNewWindow={() => onOpenSessionInNewWindow(s.id)}
                      onRename={(title) => onRename(s.id, title)}
                      onRequestDelete={() => setConfirmDelete(s)}
                      onToggleStar={() => onToggleStar(s.id, !s.starred)}
                      onToggleArchive={() => onToggleArchive(s.id, !s.archived)}
                      onSetFolder={(folder) => onSetFolder(s.id, folder)}
                    />
                  ))}
                </ul>
              )}
            </SidebarSection>
          ) : (
            groups.map((group, i) => (
              <SidebarSection
                key={group.folder ?? `__default-${i}`}
                title={group.folder || (filter === "archived" ? t("sidebar.tab.archived") : filter === "starred" ? t("sidebar.tab.starred") : t("sidebar.section.conversations"))}
              >
                {group.sessions.length === 0 ? (
                  <div className="sidebar-empty">{t("sidebar.no_chats")}</div>
                ) : (
                  <ul className="session-list">
                    {group.sessions.map((s) => (
                      <SessionRow
                        key={s.id}
                        session={s}
                        active={s.id === currentSessionId}
                        canOpenWindow={canOpenWindow}
                        onSelect={() => onSelectSession(s.id)}
                        onOpenInNewWindow={() => onOpenSessionInNewWindow(s.id)}
                        onRename={(title) => onRename(s.id, title)}
                        onRequestDelete={() => setConfirmDelete(s)}
                        onToggleStar={() => onToggleStar(s.id, !s.starred)}
                        onToggleArchive={() => onToggleArchive(s.id, !s.archived)}
                        onSetFolder={(folder) => onSetFolder(s.id, folder)}
                      />
                    ))}
                  </ul>
                )}
              </SidebarSection>
            ))
          )}
          </div>

          <nav className="sidebar-nav">
            {(() => {
              // Home link → ald3.com services hub. Hidden on wizerith.ai
              // so the work-side tenant has no path back to the personal-
              // side landing (wizerith-isolation rule).
              const host = typeof window !== "undefined" ? window.location.hostname : "";
              const onAld3 = host === "ald3.com" || host.endsWith(".ald3.com");
              if (!onAld3) return null;
              return (
                <a
                  className="sidebar-nav-link"
                  href="https://ald3.com/"
                  title="ald3.com services hub"
                >
                  Home
                </a>
              );
            })()}
            {isAdmin && (
              <>
                <button
                  type="button"
                  className="sidebar-nav-link"
                  onClick={onOpenAgents}
                  title={t("sidebar.nav.agents")}
                >
                  {t("sidebar.nav.agents")}
                </button>
                <button
                  type="button"
                  className="sidebar-nav-link"
                  onClick={onOpenUsage}
                  title={t("sidebar.nav.usage")}
                >
                  {t("sidebar.nav.usage")}
                </button>
              </>
            )}
            <button
              type="button"
              className="sidebar-nav-link"
              onClick={onOpenAbout}
              title={t("sidebar.nav.about_title")}
            >
              {t("sidebar.nav.about")}
            </button>
            <a
              className="sidebar-nav-link"
              href={terminalHref}
              target="_blank"
              rel="noopener noreferrer"
              title={t("sidebar.nav.terminal_title")}
            >
              {t("sidebar.nav.terminal")}
            </a>
            <button
              type="button"
              className="sidebar-nav-link"
              onClick={onOpenSettings}
              title={t("sidebar.nav.settings")}
            >
              {t("sidebar.nav.settings")}
            </button>
            {(() => {
              // Tenant-aware sign-out:
              //   ald3 — sidebar owns the logout button; chat is the only
              //          chrome on chat.ald3.com so we render it here.
              //          POSTs /api/auth/logout (local auth, no CF).
              //   wizerith — the shared topbar (rendered by
              //          /_theme/wizerith-theme.js) already exposes a
              //          sign-out icon next to the user pill, so a sidebar
              //          logout would just duplicate it. The pre-migration
              //          variant pointed at /cdn-cgi/access/logout (CF
              //          Access) which is itself deprecated after the
              //          passwordless move. Hide both on wizerith hosts.
              const host = typeof window !== "undefined" ? window.location.hostname : "";
              const onAld3 = host === "ald3.com" || host.endsWith(".ald3.com");
              const onWizerith = host === "wizerith.ai" || host.endsWith(".wizerith.ai");
              if (onWizerith) return null;
              const title = me?.email
                ? t("sidebar.nav.logout_title", { email: me.email })
                : t("sidebar.nav.logout");
              if (onAld3) {
                return (
                  <button
                    type="button"
                    className="sidebar-nav-link"
                    title={title}
                    onClick={async () => {
                      try {
                        await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
                      } catch { /* ignore */ }
                      window.location.reload();
                    }}
                  >
                    {t("sidebar.nav.logout")}
                  </button>
                );
              }
              // Fallback for any host that's neither ald3 nor wizerith —
              // shouldn't reach in production but keeps the local-auth
              // flow intact rather than the legacy CF Access redirect.
              return (
                <button
                  type="button"
                  className="sidebar-nav-link"
                  title={title}
                  onClick={async () => {
                    try {
                      await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
                    } catch { /* ignore */ }
                    window.location.reload();
                  }}
                >
                  {t("sidebar.nav.logout")}
                </button>
              );
            })()}
          </nav>
        </>
      )}
      {confirmDelete && (
        <ConfirmDialog
          title={t("session.confirm.delete_title")}
          message={t("session.confirm.delete", {
            title: confirmDelete.title || t("session.untitled"),
          })}
          confirmLabel={t("session.menu.delete")}
          cancelLabel={t("confirm.cancel")}
          destructive
          onConfirm={() => {
            onDelete(confirmDelete.id);
            setConfirmDelete(null);
          }}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </aside>
  );
}

interface SidebarSectionProps {
  title: string;
  children: React.ReactNode;
}

function SidebarSection({ title, children }: SidebarSectionProps): JSX.Element {
  return (
    <section className="sidebar-section">
      <h2 className="sidebar-section-title">{title}</h2>
      {children}
    </section>
  );
}

interface SessionRowProps {
  session: SessionSummary;
  snippet?: string;
  active: boolean;
  /** False when the max number of windows is already open (disables the
   *  "Open in new window" menu item). */
  canOpenWindow: boolean;
  onSelect: () => void;
  onOpenInNewWindow: () => void;
  onRename: (title: string) => void;
  /** Ask the parent to open the delete-confirmation modal for this row. */
  onRequestDelete: () => void;
  onToggleStar: () => void;
  onToggleArchive: () => void;
  onSetFolder: (folder: string | null) => void;
}

function SessionRow(props: SessionRowProps): JSX.Element {
  const {
    session,
    snippet,
    active,
    canOpenWindow,
    onSelect,
    onOpenInNewWindow,
    onRename,
    onRequestDelete,
    onToggleStar,
    onToggleArchive,
    onSetFolder,
  } = props;
  const t = useT();
  const [editing, setEditing] = useState<boolean>(false);
  const [draft, setDraft] = useState<string>(session.title || "");
  const [menuOpen, setMenuOpen] = useState<boolean>(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const el = menuRef.current;
      if (el && !el.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    // pointerdown beats click so clicking elsewhere closes before that
    // element's own click handler runs (e.g. selecting a different row).
    window.addEventListener("pointerdown", onDocClick);
    return () => window.removeEventListener("pointerdown", onDocClick);
  }, [menuOpen]);

  const startRename = useCallback(() => {
    setDraft(session.title || "");
    setEditing(true);
  }, [session.title]);

  const commitRename = useCallback(() => {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== session.title) {
      onRename(trimmed);
    }
    setEditing(false);
  }, [draft, session.title, onRename]);

  const cancelRename = useCallback(() => {
    setEditing(false);
    setDraft(session.title || "");
  }, [session.title]);

  const promptFolder = useCallback(() => {
    const next = window.prompt(
      t("session.prompt.folder"),
      session.folder || "",
    );
    if (next === null) return;
    const trimmed = next.trim();
    onSetFolder(trimmed || null);
    setMenuOpen(false);
  }, [session.folder, onSetFolder, t]);

  return (
    <li className={`session-row ${active ? "is-active" : ""}`}>
      <a
        className="session-main"
        href={`?s=${encodeURIComponent(session.id)}`}
        onClick={(e) => {
          if (isModifierClick(e)) return;
          if (editing) {
            e.preventDefault();
            return;
          }
          e.preventDefault();
          onSelect();
        }}
        title={session.title || t("session.untitled")}
      >
        {editing ? (
          <input
            className="session-rename-input"
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitRename();
              if (e.key === "Escape") cancelRename();
            }}
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          <>
            <span className="session-title">
              {session.title || <span className="muted">{t("session.untitled")}</span>}
              {session.workspace === "shared" && (
                <span
                  className="session-workspace-badge"
                  title={t("sidebar.workspace.title.shared")}
                  aria-label={t("sidebar.workspace.shared")}
                >
                  {t("session.workspace.shared_badge")}
                </span>
              )}
            </span>
            {snippet && <span className="session-snippet">{snippet}</span>}
            <span className="session-ts">{formatRelative(session.updated_at, t)}</span>
          </>
        )}
      </a>
      {!editing && (
        <span className="session-actions">
          <button
            type="button"
            className={`session-action session-star ${session.starred ? "is-on" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              onToggleStar();
            }}
            aria-label={session.starred ? t("session.unstar") : t("session.star")}
            title={session.starred ? t("session.unstar") : t("session.star")}
          >
            {session.starred ? "★" : "☆"}
          </button>
          <button
            type="button"
            className={`session-action session-more ${menuOpen ? "is-active" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              setMenuOpen((o) => !o);
            }}
            aria-label={t("session.more")}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            title={t("session.more")}
          >
            ⋯
          </button>
          {menuOpen && (
            <div
              ref={menuRef}
              className="session-menu"
              role="menu"
              onClick={(e) => e.stopPropagation()}
            >
              <button
                type="button"
                className="session-menu-item"
                disabled={!canOpenWindow}
                title={canOpenWindow ? undefined : t("sidebar.new_window_max")}
                onClick={() => {
                  setMenuOpen(false);
                  onOpenInNewWindow();
                }}
              >
                {t("session.menu.open_in_new_window")}
              </button>
              <button
                type="button"
                className="session-menu-item"
                onClick={() => {
                  setMenuOpen(false);
                  startRename();
                }}
              >
                {t("session.menu.rename")}
              </button>
              <button
                type="button"
                className="session-menu-item"
                onClick={promptFolder}
              >
                {session.folder
                  ? t("session.menu.folder_label", { name: session.folder })
                  : t("session.menu.set_folder")}
              </button>
              <button
                type="button"
                className="session-menu-item"
                onClick={() => {
                  setMenuOpen(false);
                  onToggleArchive();
                }}
              >
                {session.archived ? t("session.menu.unarchive") : t("session.menu.archive")}
              </button>
              <a
                className="session-menu-item"
                href={exportSessionUrl(session.id, "md")}
                download
                onClick={() => setMenuOpen(false)}
              >
                {t("session.menu.export_md")}
              </a>
              <a
                className="session-menu-item"
                href={exportSessionUrl(session.id, "json")}
                download
                onClick={() => setMenuOpen(false)}
              >
                {t("session.menu.export_json")}
              </a>
              <button
                type="button"
                className="session-menu-item is-destructive"
                onClick={() => {
                  setMenuOpen(false);
                  onRequestDelete();
                }}
              >
                {t("session.menu.delete")}
              </button>
            </div>
          )}
        </span>
      )}
    </li>
  );
}

function formatRelative(iso: string, t: TFunc): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = Date.now();
  const diffMs = now - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return t("time.just_now");
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return t("time.minutes_ago", { count: diffMin });
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return t("time.hours_ago", { count: diffHr });
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay === 1) return t("time.yesterday");
  if (diffDay < 7) return t("time.days_ago", { count: diffDay });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
