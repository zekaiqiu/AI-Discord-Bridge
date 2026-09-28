import { IDockviewPanelProps } from "dockview-react";
import { Terminal } from "../components/Terminal";
import { useWorkspace } from "../workspace";

// Each terminal panel mounts its own xterm + WS to /api/terminal. Multiple
// panels = multiple shells. The Terminal component is mount-once-on-first
// -visible; we pass visible=true unconditionally since dockview keeps the
// DOM mounted while the panel exists (even when its tab is inactive,
// dockview shows it as a hidden child — equivalent to the old
// `display:none` we used in BottomPanel).

export function TerminalPanel(props: IDockviewPanelProps<{ shellId: number }>) {
  const ws = useWorkspace();
  void props.params.shellId;
  return <Terminal visible={true} theme={ws.theme} />;
}
