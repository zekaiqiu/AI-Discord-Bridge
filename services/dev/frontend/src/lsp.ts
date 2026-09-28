// Minimal LSP client speaking raw JSON-RPC over WebSocket to the
// dev-wizerith backend, which bridges to pyright-langserver inside the
// user's container.
//
// Why not monaco-languageclient: that library needs @codingame/monaco-
// vscode-api services (configuration, theme, textmate, workspace) wired
// up before it'll start. That's the right tool the day we want full LSP
// surface — diagnostics, code actions, signature help, refactorings, etc.
// — and willing to swap @monaco-editor/react for a direct monaco-editor
// integration. For "autocomplete + hover + go-to-def + squiggles" we get
// the same UX from Monaco's native provider APIs, ~50× less code, and
// keep the existing simple editor mount.
//
// LSP features wired in v1:
//   - textDocument/completion          → registerCompletionItemProvider
//   - textDocument/hover               → registerHoverProvider
//   - textDocument/definition          → registerDefinitionProvider
//   - textDocument/publishDiagnostics  → editor.setModelMarkers
//
// Not wired (M3+): references, document symbols, signature help,
// formatting, code actions, rename, semantic tokens.

import type { Monaco } from "@monaco-editor/react";

type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [k: string]: JsonValue };

type LspMessage = {
  jsonrpc: "2.0";
  id?: number | string;
  method?: string;
  params?: JsonValue;
  result?: JsonValue;
  error?: { code: number; message: string; data?: JsonValue };
};

export type LspStatus = "connecting" | "ready" | "closed" | "error";

const WORKSPACE_URI = "file:///workspace";

// Convert a workspace-relative path like "foo/bar.py" to a file:// URI under
// /workspace. Strips any leading slashes — paths from the file tree are
// already relative.
export function pathToUri(workspacePath: string): string {
  const rel = workspacePath.replace(/^\/+/, "");
  return `${WORKSPACE_URI}/${rel}`;
}

export function uriToPath(uri: string): string | null {
  if (!uri.startsWith(`${WORKSPACE_URI}/`)) return null;
  return uri.substring(`${WORKSPACE_URI}/`.length);
}


export class LspClient {
  private ws: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<
    number,
    { resolve: (v: JsonValue) => void; reject: (e: Error) => void }
  >();
  private status: LspStatus = "connecting";
  private statusListeners = new Set<(s: LspStatus) => void>();
  private initialized = false;
  private initWaiters: Array<() => void> = [];
  private monaco: Monaco;
  private disposables: Array<{ dispose: () => void }> = [];
  // Documents currently open in the editor. We track version per URI so
  // didChange increments correctly.
  private openDocs = new Map<string, number>();

  constructor(monaco: Monaco) {
    this.monaco = monaco;
  }

  onStatus(fn: (s: LspStatus) => void): () => void {
    this.statusListeners.add(fn);
    fn(this.status);
    return () => this.statusListeners.delete(fn);
  }

  private setStatus(s: LspStatus) {
    this.status = s;
    this.statusListeners.forEach((fn) => fn(s));
  }

  // -------------------------------------------------------------
  // Connection lifecycle.
  // -------------------------------------------------------------

  async start(): Promise<void> {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/api/lsp/pyright`;
    this.ws = new WebSocket(url);
    this.ws.onopen = () => this.onOpen();
    this.ws.onmessage = (e) => this.onMessage(e);
    this.ws.onclose = () => {
      this.setStatus("closed");
      // Reject any pending requests so callers don't hang forever.
      for (const { reject } of this.pending.values()) {
        reject(new Error("LSP connection closed"));
      }
      this.pending.clear();
    };
    this.ws.onerror = () => {
      this.setStatus("error");
    };
    this.registerProviders();
  }

  private async onOpen() {
    try {
      const initRes = (await this.request("initialize", {
        processId: null,
        clientInfo: { name: "dev-wizerith", version: "0.1.0" },
        rootUri: WORKSPACE_URI,
        capabilities: {
          textDocument: {
            synchronization: { dynamicRegistration: false },
            completion: {
              completionItem: {
                snippetSupport: false,
                documentationFormat: ["markdown", "plaintext"],
              },
              contextSupport: true,
            },
            hover: { contentFormat: ["markdown", "plaintext"] },
            definition: { linkSupport: false },
            publishDiagnostics: {},
          },
          workspace: {
            workspaceFolders: true,
            configuration: true,
          },
        },
        workspaceFolders: [
          { uri: WORKSPACE_URI, name: "workspace" },
        ],
        initializationOptions: {
          // Pyright reads typeCheckingMode from settings; we send via
          // workspace/configuration too, but the initializationOptions
          // are read earliest so set it here too.
          // "off" keeps the LSP fast and noise-free for v1 — diagnostics
          // are still emitted for parse errors and undefined names but we
          // skip the heavier strict-mode work.
          settings: {
            python: {
              analysis: {
                typeCheckingMode: "basic",
                autoSearchPaths: true,
                useLibraryCodeForTypes: true,
                diagnosticMode: "openFilesOnly",
              },
            },
          },
        },
      })) as { capabilities: JsonValue };
      void initRes;
      this.notify("initialized", {});
      this.initialized = true;
      this.setStatus("ready");
      const waiters = this.initWaiters.splice(0);
      waiters.forEach((w) => w());
    } catch (err) {
      // Initialize failed — surface error and shut down.
      // eslint-disable-next-line no-console
      console.error("LSP initialize failed:", err);
      this.setStatus("error");
    }
  }

  private waitForInit(): Promise<void> {
    if (this.initialized) return Promise.resolve();
    return new Promise((resolve) => this.initWaiters.push(resolve));
  }

  // -------------------------------------------------------------
  // JSON-RPC plumbing.
  // -------------------------------------------------------------

  private onMessage(e: MessageEvent) {
    let msg: LspMessage;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) {
      // Response to one of our requests.
      const pend = this.pending.get(msg.id as number);
      if (!pend) return;
      this.pending.delete(msg.id as number);
      if (msg.error) {
        pend.reject(new Error(msg.error.message));
      } else {
        pend.resolve(msg.result ?? null);
      }
      return;
    }
    if (msg.method) {
      // Server-initiated request or notification.
      this.handleServerMessage(msg);
    }
  }

  private handleServerMessage(msg: LspMessage) {
    if (msg.method === "textDocument/publishDiagnostics") {
      this.applyDiagnostics(msg.params as PublishDiagnosticsParams);
      return;
    }
    if (msg.method === "workspace/configuration") {
      // Pyright requests our config. Reply with the same settings as
      // initializationOptions — single source of truth.
      const items = ((msg.params as { items?: Array<{ section?: string }> })?.items ?? []);
      const result: JsonValue = items.map((item) => {
        if (item.section === "python" || item.section === "python.analysis") {
          return {
            analysis: {
              typeCheckingMode: "basic",
              autoSearchPaths: true,
              useLibraryCodeForTypes: true,
              diagnosticMode: "openFilesOnly",
            },
          } as JsonValue;
        }
        return {} as JsonValue;
      });
      this.reply(msg.id as number, result);
      return;
    }
    if (msg.method === "client/registerCapability" ||
        msg.method === "client/unregisterCapability") {
      // We don't dynamically register capabilities — acknowledge to keep
      // pyright happy.
      this.reply(msg.id as number, null);
      return;
    }
    if (msg.method === "window/logMessage" || msg.method === "window/showMessage") {
      // eslint-disable-next-line no-console
      console.log("[pyright]", (msg.params as any)?.message ?? msg.params);
      return;
    }
    // Other server requests we don't handle yet — reply with null so
    // pyright doesn't block waiting.
    if (msg.id !== undefined) {
      this.reply(msg.id as number, null);
    }
  }

  private request(method: string, params: JsonValue): Promise<JsonValue> {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("LSP socket not open"));
        return;
      }
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      const msg: LspMessage = { jsonrpc: "2.0", id, method, params };
      this.ws.send(JSON.stringify(msg));
    });
  }

  private notify(method: string, params: JsonValue): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const msg: LspMessage = { jsonrpc: "2.0", method, params };
    this.ws.send(JSON.stringify(msg));
  }

  private reply(id: number, result: JsonValue): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const msg: LspMessage = { jsonrpc: "2.0", id, result };
    this.ws.send(JSON.stringify(msg));
  }

  // -------------------------------------------------------------
  // Document sync.
  // -------------------------------------------------------------

  async didOpen(path: string, text: string): Promise<void> {
    await this.waitForInit();
    const uri = pathToUri(path);
    this.openDocs.set(uri, 1);
    this.notify("textDocument/didOpen", {
      textDocument: {
        uri,
        languageId: "python",
        version: 1,
        text,
      },
    });
  }

  async didChange(path: string, text: string): Promise<void> {
    await this.waitForInit();
    const uri = pathToUri(path);
    const prev = this.openDocs.get(uri) ?? 0;
    const version = prev + 1;
    this.openDocs.set(uri, version);
    // Full-document sync — simpler than incremental and pyright handles it.
    this.notify("textDocument/didChange", {
      textDocument: { uri, version },
      contentChanges: [{ text }],
    });
  }

  async didClose(path: string): Promise<void> {
    const uri = pathToUri(path);
    if (!this.openDocs.delete(uri)) return;
    this.notify("textDocument/didClose", {
      textDocument: { uri },
    });
    // Clear any diagnostics for the closed file.
    const model = this.findModel(uri);
    if (model) {
      this.monaco.editor.setModelMarkers(model, "pyright", []);
    }
  }

  // -------------------------------------------------------------
  // Monaco provider registration.
  // -------------------------------------------------------------

  private registerProviders() {
    const selector = "python";

    this.disposables.push(
      this.monaco.languages.registerCompletionItemProvider(selector, {
        triggerCharacters: [".", "(", ",", "=", "[", '"', "'", " "],
        provideCompletionItems: async (model, position) => {
          if (this.status !== "ready") return { suggestions: [] };
          const uri = model.uri.toString();
          if (!uri.startsWith(WORKSPACE_URI)) return { suggestions: [] };
          try {
            const result = (await this.request("textDocument/completion", {
              textDocument: { uri },
              position: { line: position.lineNumber - 1, character: position.column - 1 },
              context: { triggerKind: 1 },
            })) as CompletionList | CompletionItem[] | null;
            if (!result) return { suggestions: [] };
            const items = Array.isArray(result) ? result : (result.items ?? []);
            const word = model.getWordUntilPosition(position);
            const range = new this.monaco.Range(
              position.lineNumber, word.startColumn,
              position.lineNumber, word.endColumn,
            );
            return {
              suggestions: items.map((it) => this.toMonacoCompletion(it, range)),
              incomplete: !Array.isArray(result) && result.isIncomplete === true,
            };
          } catch {
            return { suggestions: [] };
          }
        },
      }),
    );

    this.disposables.push(
      this.monaco.languages.registerHoverProvider(selector, {
        provideHover: async (model, position) => {
          if (this.status !== "ready") return null;
          const uri = model.uri.toString();
          if (!uri.startsWith(WORKSPACE_URI)) return null;
          try {
            const result = (await this.request("textDocument/hover", {
              textDocument: { uri },
              position: { line: position.lineNumber - 1, character: position.column - 1 },
            })) as Hover | null;
            if (!result || !result.contents) return null;
            const contents = this.flattenHoverContents(result.contents);
            return { contents, range: result.range ? this.lspRangeToMonaco(result.range) : undefined };
          } catch {
            return null;
          }
        },
      }),
    );

    this.disposables.push(
      this.monaco.languages.registerDefinitionProvider(selector, {
        provideDefinition: async (model, position) => {
          if (this.status !== "ready") return null;
          const uri = model.uri.toString();
          if (!uri.startsWith(WORKSPACE_URI)) return null;
          try {
            const result = (await this.request("textDocument/definition", {
              textDocument: { uri },
              position: { line: position.lineNumber - 1, character: position.column - 1 },
            })) as Location | Location[] | null;
            if (!result) return null;
            const arr = Array.isArray(result) ? result : [result];
            return arr.map((loc) => ({
              uri: this.monaco.Uri.parse(loc.uri),
              range: this.lspRangeToMonaco(loc.range),
            }));
          } catch {
            return null;
          }
        },
      }),
    );
  }

  // -------------------------------------------------------------
  // Diagnostics → Monaco markers.
  // -------------------------------------------------------------

  private applyDiagnostics(params: PublishDiagnosticsParams) {
    const model = this.findModel(params.uri);
    if (!model) return;
    const markers = (params.diagnostics ?? []).map((d) => ({
      startLineNumber: d.range.start.line + 1,
      startColumn: d.range.start.character + 1,
      endLineNumber: d.range.end.line + 1,
      endColumn: d.range.end.character + 1,
      message: d.message,
      severity: this.lspSeverityToMonaco(d.severity ?? 1),
      source: d.source ?? "pyright",
    }));
    this.monaco.editor.setModelMarkers(model, "pyright", markers);
  }

  private findModel(uri: string): import("monaco-editor").editor.ITextModel | null {
    const monacoUri = this.monaco.Uri.parse(uri);
    return this.monaco.editor.getModel(monacoUri);
  }

  // -------------------------------------------------------------
  // Conversion helpers.
  // -------------------------------------------------------------

  private lspRangeToMonaco(r: LspRange): import("monaco-editor").IRange {
    return {
      startLineNumber: r.start.line + 1,
      startColumn: r.start.character + 1,
      endLineNumber: r.end.line + 1,
      endColumn: r.end.character + 1,
    };
  }

  private lspSeverityToMonaco(s: number): number {
    // LSP severity: 1=Error, 2=Warning, 3=Info, 4=Hint
    // Monaco severity: 8=Error, 4=Warning, 2=Info, 1=Hint
    switch (s) {
      case 1: return this.monaco.MarkerSeverity.Error;
      case 2: return this.monaco.MarkerSeverity.Warning;
      case 3: return this.monaco.MarkerSeverity.Info;
      default: return this.monaco.MarkerSeverity.Hint;
    }
  }

  private toMonacoCompletion(
    item: CompletionItem,
    range: import("monaco-editor").IRange,
  ): import("monaco-editor").languages.CompletionItem {
    const kindMap: Record<number, number> = {
      1: this.monaco.languages.CompletionItemKind.Text,
      2: this.monaco.languages.CompletionItemKind.Method,
      3: this.monaco.languages.CompletionItemKind.Function,
      4: this.monaco.languages.CompletionItemKind.Constructor,
      5: this.monaco.languages.CompletionItemKind.Field,
      6: this.monaco.languages.CompletionItemKind.Variable,
      7: this.monaco.languages.CompletionItemKind.Class,
      8: this.monaco.languages.CompletionItemKind.Interface,
      9: this.monaco.languages.CompletionItemKind.Module,
      10: this.monaco.languages.CompletionItemKind.Property,
      11: this.monaco.languages.CompletionItemKind.Unit,
      12: this.monaco.languages.CompletionItemKind.Value,
      13: this.monaco.languages.CompletionItemKind.Enum,
      14: this.monaco.languages.CompletionItemKind.Keyword,
      15: this.monaco.languages.CompletionItemKind.Snippet,
      16: this.monaco.languages.CompletionItemKind.Color,
      17: this.monaco.languages.CompletionItemKind.File,
      18: this.monaco.languages.CompletionItemKind.Reference,
      19: this.monaco.languages.CompletionItemKind.Folder,
      20: this.monaco.languages.CompletionItemKind.EnumMember,
      21: this.monaco.languages.CompletionItemKind.Constant,
      22: this.monaco.languages.CompletionItemKind.Struct,
      23: this.monaco.languages.CompletionItemKind.Event,
      24: this.monaco.languages.CompletionItemKind.Operator,
      25: this.monaco.languages.CompletionItemKind.TypeParameter,
    };
    const documentation =
      typeof item.documentation === "string"
        ? item.documentation
        : item.documentation?.value;
    return {
      label: item.label,
      kind: kindMap[item.kind ?? 6] ?? this.monaco.languages.CompletionItemKind.Text,
      detail: item.detail,
      documentation: documentation
        ? { value: documentation, isTrusted: false }
        : undefined,
      insertText: item.insertText ?? item.label,
      sortText: item.sortText,
      filterText: item.filterText,
      range,
    };
  }

  private flattenHoverContents(
    contents: Hover["contents"],
  ): import("monaco-editor").IMarkdownString[] {
    if (typeof contents === "string") {
      return [{ value: contents }];
    }
    if (Array.isArray(contents)) {
      return contents.map((c) => {
        if (typeof c === "string") return { value: c };
        if ("language" in c) return { value: "```" + c.language + "\n" + c.value + "\n```" };
        return { value: c.value };
      });
    }
    // MarkupContent | MarkedString
    if ("kind" in contents) {
      return [{ value: contents.value }];
    }
    if ("language" in contents) {
      return [{ value: "```" + contents.language + "\n" + contents.value + "\n```" }];
    }
    return [{ value: String(contents) }];
  }

  // -------------------------------------------------------------
  // Teardown.
  // -------------------------------------------------------------

  dispose() {
    this.disposables.forEach((d) => {
      try { d.dispose(); } catch { /* ignore */ }
    });
    this.disposables = [];
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
  }
}


// LSP type sketches — only the fields we read.

type LspPosition = { line: number; character: number };
type LspRange = { start: LspPosition; end: LspPosition };

type CompletionItem = {
  label: string;
  kind?: number;
  detail?: string;
  documentation?: string | { kind: string; value: string };
  insertText?: string;
  sortText?: string;
  filterText?: string;
};

type CompletionList = {
  isIncomplete?: boolean;
  items: CompletionItem[];
};

type Hover = {
  contents:
    | string
    | { kind: "plaintext" | "markdown"; value: string }
    | { language: string; value: string }
    | Array<string | { language: string; value: string } | { value: string }>;
  range?: LspRange;
};

type Location = {
  uri: string;
  range: LspRange;
};

type Diagnostic = {
  range: LspRange;
  severity?: number;
  source?: string;
  message: string;
};

type PublishDiagnosticsParams = {
  uri: string;
  diagnostics: Diagnostic[];
};
