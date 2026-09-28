import Editor, { loader } from "@monaco-editor/react";
import { useEffect } from "react";
import { pathToUri } from "../lsp";

type Props = {
  path: string;
  value: string;
  onChange: (v: string) => void;
  theme: "dark" | "light";
};

// Map file extension to Monaco language. The defaults Monaco infers from
// filename are usually right; we override only for things it gets wrong
// (e.g. .ipynb → json, not its own thing — but we don't open .ipynb in v1).
function inferLanguage(path: string): string {
  const name = path.split("/").pop() ?? "";
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  switch (ext) {
    case "py":
      return "python";
    case "ts":
    case "tsx":
      return "typescript";
    case "js":
    case "jsx":
      return "javascript";
    case "json":
      return "json";
    case "md":
      return "markdown";
    case "yml":
    case "yaml":
      return "yaml";
    case "toml":
      return "ini";
    case "html":
      return "html";
    case "css":
      return "css";
    case "sh":
    case "bash":
      return "shell";
    case "sql":
      return "sql";
    case "":
      return "plaintext";
    default:
      return "plaintext";
  }
}

// Define Wizerith-tinted themes on first mount. Monaco loads its own theme
// system; we register ours so the editor matches the chrome.
let themesRegistered = false;
function registerThemes() {
  if (themesRegistered) return;
  themesRegistered = true;
  loader.init().then((monaco) => {
    monaco.editor.defineTheme("wizerith-dark", {
      base: "vs-dark",
      inherit: true,
      rules: [],
      colors: {
        "editor.background": "#000000",
        "editor.foreground": "#f5f5f7",
        "editorLineNumber.foreground": "#48484a",
        "editorLineNumber.activeForeground": "#0a84ff",
        "editor.selectionBackground": "#264f78",
        "editor.lineHighlightBackground": "#1c1c1e",
        "editorCursor.foreground": "#0a84ff",
        "editorIndentGuide.background": "#2c2c2e",
      },
    });
    monaco.editor.defineTheme("wizerith-light", {
      base: "vs",
      inherit: true,
      rules: [],
      colors: {
        "editor.background": "#f5f5f7",
        "editor.foreground": "#1d1d1f",
        "editorLineNumber.foreground": "#98989d",
        "editorLineNumber.activeForeground": "#0071e3",
        "editor.lineHighlightBackground": "#ececef",
        "editorCursor.foreground": "#0071e3",
      },
    });
  });
}

export function EditorPane({ path, value, onChange, theme }: Props) {
  useEffect(() => {
    registerThemes();
  }, []);

  const language = inferLanguage(path);
  const monacoTheme = theme === "dark" ? "wizerith-dark" : "wizerith-light";

  return (
    <Editor
      height="100%"
      width="100%"
      // Model URI matches the LSP URI scheme (file:///workspace/<path>) so
      // textDocument/{didOpen,didChange,publishDiagnostics} target the
      // same model. Distinct per file path → preserves undo history per
      // tab.
      path={pathToUri(path)}
      language={language}
      value={value}
      theme={monacoTheme}
      onChange={(v) => onChange(v ?? "")}
      options={{
        fontSize: 13,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        renderWhitespace: "selection",
        smoothScrolling: true,
        tabSize: 4,
        insertSpaces: true,
        wordWrap: "off",
        automaticLayout: true,
      }}
    />
  );
}
