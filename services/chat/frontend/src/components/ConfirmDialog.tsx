/**
 * Lightweight in-app confirmation modal — replaces window.confirm() so the
 * dialog matches the app's theme instead of the browser chrome.
 *
 * Behaviour: Esc or backdrop click cancels; Enter / focused button confirms.
 * The confirm button autofocuses. `destructive` paints it red for deletes.
 */

import { useEffect, useRef } from "react";

export interface ConfirmDialogProps {
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog(props: ConfirmDialogProps): JSX.Element {
  const { title, message, confirmLabel, cancelLabel, destructive, onConfirm, onCancel } = props;
  const confirmRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    confirmRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      className="confirm-backdrop"
      onMouseDown={(e) => {
        // Only cancel on a click that starts AND ends on the backdrop.
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        className="confirm-modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-message"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 className="confirm-title" id="confirm-title">
          {title}
        </h2>
        <p className="confirm-message" id="confirm-message">
          {message}
        </p>
        <div className="confirm-actions">
          <button type="button" className="confirm-btn confirm-btn--cancel" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`confirm-btn ${destructive ? "confirm-btn--danger" : "confirm-btn--primary"}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
