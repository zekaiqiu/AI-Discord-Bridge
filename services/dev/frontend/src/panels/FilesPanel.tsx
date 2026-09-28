import { FileTree } from "../components/FileTree";
import { useWorkspace } from "../workspace";

export function FilesPanel() {
  const ws = useWorkspace();
  return <FileTree onOpen={(p) => void ws.openFile(p)} />;
}
