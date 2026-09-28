import { useEffect, useRef } from "react";

export type OutputLine = {
  kind: string; // stdout | stderr | system | exit
  text: string;
  ts: number;
};

type Props = {
  lines: OutputLine[];
  running: boolean;
};

export function OutputPane({ lines, running }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  // Auto-scroll to bottom when new lines arrive — only if we're already
  // near the bottom, so a user reading scrollback isn't yanked away.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="output">
      <div className="output-header">
        <span>Output</span>
        {running && <span className="status-pill running">● running</span>}
      </div>
      <div className="output-body" ref={ref}>
        {lines.length === 0 ? (
          <div className="output-empty">
            No output yet. Press <kbd>Cmd/Ctrl</kbd>+<kbd>Enter</kbd> to run.
          </div>
        ) : (
          lines.map((l, i) => (
            <div key={i} className={`output-line ${l.kind}`}>
              {l.text.replace(/\n$/, "")}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
