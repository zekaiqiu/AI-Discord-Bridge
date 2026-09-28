import { IDockviewPanelProps } from "dockview-react";
import { useEffect } from "react";
import { EditorPane } from "../components/EditorPane";
import { Viewer, viewerKindFor } from "../components/Viewer";
import { useWorkspace } from "../workspace";

// One editor panel = one open file. The panel id is the workspace-relative
// path (e.g. "foo/bar.py"); we read content from the shared tabs[] in
// the workspace context, write back via updateContent on edits.
//
// When the panel becomes active in its tab group, we mirror that into
// the workspace's activePath so the TopBar and Run command target the
// right file. dockview fires onDidActiveChange via the api.

export function EditorPanel(props: IDockviewPanelProps<{ path: string }>) {
  const { path } = props.params;
  const ws = useWorkspace();
  const tab = ws.tabs.find((t) => t.path === path);

  // Sync active focus into workspace state.
  useEffect(() => {
    const dispose = props.api.onDidActiveChange((evt) => {
      if (evt.isActive) ws.setActivePath(path);
    });
    if (props.api.isActive) ws.setActivePath(path);
    return () => dispose.dispose();
  }, [path]);

  // Panel→tabs[] removal is handled at the dockview-instance level in
  // App.tsx via `dock.onDidRemovePanel`. We keep the dispose hook local
  // to active-state sync (above) only.

  if (!tab) {
    return <div className="panel-empty">file not in workspace state</div>;
  }
  if (viewerKindFor(tab.path) !== "text") {
    return <Viewer path={tab.path} theme={ws.theme} />;
  }
  return (
    <EditorPane
      path={tab.path}
      value={tab.content}
      onChange={(v) => ws.updateContent(tab.path, v)}
      theme={ws.theme}
    />
  );
}
