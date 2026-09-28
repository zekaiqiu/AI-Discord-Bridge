import { OutputPane } from "../components/OutputPane";
import { useWorkspace } from "../workspace";

export function OutputPanel() {
  const ws = useWorkspace();
  return <OutputPane lines={ws.outputLines} running={ws.running} />;
}
