// Shared workspace state for all dockview panels.
//
// Why a context: dockview panels are constructed by the library with only
// a `params` object (which must be serializable for layout persistence).
// We can't pass React refs or callbacks through params, so live IDE state
// — open tabs, content per file, the LSP client ref, job output, theme —
// is shared via context. Panel components useWorkspace() to read it.

import {
  createContext,
  Dispatch,
  ReactNode,
  RefObject,
  SetStateAction,
  useContext,
} from "react";
import { IDEState, Me } from "./api";
import { LspClient, LspStatus } from "./lsp";
import { OutputLine } from "./components/OutputPane";

export type Tab = {
  path: string;
  active: boolean;
  cursor_line?: number;
  cursor_col?: number;
  content: string;
  savedContent: string;
};

export type WorkspaceCtx = {
  me: Me;
  theme: "dark" | "light";

  // Tabs / editor state.
  tabs: Tab[];
  activePath: string | null;
  openFile: (path: string) => Promise<void>;
  closeTab: (path: string) => void;
  updateContent: (path: string, content: string) => void;
  saveActive: () => Promise<void>;
  setActivePath: (path: string | null) => void;

  // Run state.
  outputLines: OutputLine[];
  running: boolean;
  currentJobId: string | null;
  runActive: () => Promise<void>;
  killActive: () => Promise<void>;
  clearOutput: () => void;

  // LSP.
  lspStatus: LspStatus;
  lspRef: RefObject<LspClient | null>;

  // Persisted state hook so panels (e.g. Problems) can subscribe to or
  // mutate layout state if they need to. For now: read-only.
  ideState: IDEState | null;
  setIdeState: Dispatch<SetStateAction<IDEState | null>>;
};

const Ctx = createContext<WorkspaceCtx | null>(null);

export function WorkspaceProvider({
  value, children,
}: {
  value: WorkspaceCtx;
  children: ReactNode;
}) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): WorkspaceCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkspace called outside <WorkspaceProvider>");
  return v;
}
