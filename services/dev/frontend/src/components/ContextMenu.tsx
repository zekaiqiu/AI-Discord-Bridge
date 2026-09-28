import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type ContextMenuItem =
  | { kind: "item"; label: string; accelerator?: string; danger?: boolean; disabled?: boolean; onClick: () => void }
  | { kind: "separator" };

export type ContextMenuState = {
  x: number;
  y: number;
  items: ContextMenuItem[];
};

type Props = {
  state: ContextMenuState | null;
  onClose: () => void;
};

// Portal-rendered context menu. Body-anchored so the file tree's
// overflow:auto doesn't clip it; clamps inside the viewport so it
// stays fully visible when invoked near a screen edge. Close on Esc,
// click-outside, scroll, or right-click-elsewhere — same conventions
// as PyCharm / VS Code.
export function ContextMenu({ state, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 });

  useLayoutEffect(() => {
    if (!state) return;
    const el = ref.current;
    if (!el) return;
    const margin = 4;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    let left = state.x;
    let top = state.y;
    if (left + w + margin > window.innerWidth) left = window.innerWidth - w - margin;
    if (top + h + margin > window.innerHeight) top = window.innerHeight - h - margin;
    if (left < margin) left = margin;
    if (top < margin) top = margin;
    setPos({ left, top });
  }, [state]);

  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onScroll = () => onClose();
    document.addEventListener("keydown", onKey);
    // mousedown so the click that opens the menu (which fires mousedown
    // before contextmenu) doesn't instantly dismiss it.
    document.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("blur", onScroll);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("blur", onScroll);
    };
  }, [state, onClose]);

  if (!state) return null;

  return createPortal(
    <div
      ref={ref}
      className="context-menu"
      style={{ left: pos.left, top: pos.top, position: "fixed" }}
      onContextMenu={(e) => e.preventDefault()}
      role="menu"
    >
      {state.items.map((item, i) => {
        if (item.kind === "separator") {
          return <div key={i} className="context-menu-sep" role="separator" />;
        }
        return (
          <button
            key={i}
            type="button"
            className={`context-menu-item${item.danger ? " danger" : ""}${item.disabled ? " disabled" : ""}`}
            disabled={item.disabled}
            onClick={() => { item.onClick(); onClose(); }}
            role="menuitem"
          >
            <span className="context-menu-label">{item.label}</span>
            {item.accelerator && (
              <span className="context-menu-accel">{item.accelerator}</span>
            )}
          </button>
        );
      })}
    </div>,
    document.body,
  );
}
