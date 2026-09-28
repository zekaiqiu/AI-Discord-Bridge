// Styled-modal replacements for window.confirm / window.prompt / window.alert.
//
// Three async helpers return promises so callers can `await` them just like
// the native ones:
//
//   const ok   = await dialog.confirm({ title, message, danger });
//   const name = await dialog.prompt({ title, message, defaultValue });
//   await dialog.alert({ title, message, variant: "error" });
//
// Why a tiny pub-sub instead of a context: callers like onDuplicate /
// onRename in FileTree.tsx run inside event handlers, sometimes far from
// the component tree — handing them a context hook would force them to
// useState their own intent + a useEffect to dispatch. The imperative
// promise API is much closer to the native semantics they replace.
//
// One <DialogRoot /> is mounted at the App root; it subscribes here and
// renders whichever dialog is at the head of the queue. Queueing covers
// the "alert during another modal" case (rare, but possible if an error
// fires while a prompt is open).

import { useEffect, useRef, useState } from "react";

export type ConfirmOptions = {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
};

export type PromptOptions = {
  title?: string;
  message?: string;
  defaultValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  // Returning a string here surfaces it as an inline validation error
  // and prevents Confirm from resolving until the user fixes the input.
  validate?: (value: string) => string | null;
};

export type AlertOptions = {
  title?: string;
  message: string;
  okLabel?: string;
  variant?: "info" | "error" | "warning";
};

type ConfirmReq = { kind: "confirm"; id: number; opts: ConfirmOptions; resolve: (v: boolean) => void };
type PromptReq  = { kind: "prompt";  id: number; opts: PromptOptions;  resolve: (v: string | null) => void };
type AlertReq   = { kind: "alert";   id: number; opts: AlertOptions;   resolve: () => void };
type DialogReq  = ConfirmReq | PromptReq | AlertReq;

let nextId = 1;
const queue: DialogReq[] = [];
const listeners = new Set<() => void>();
function notify() { listeners.forEach((fn) => fn()); }

function push(req: DialogReq) {
  queue.push(req);
  notify();
}

function resolveHead(answerFn: (head: DialogReq) => void) {
  const head = queue[0];
  if (!head) return;
  answerFn(head);
  queue.shift();
  notify();
}

export const dialog = {
  confirm(opts: ConfirmOptions): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      push({ kind: "confirm", id: nextId++, opts, resolve });
    });
  },
  prompt(opts: PromptOptions): Promise<string | null> {
    return new Promise<string | null>((resolve) => {
      push({ kind: "prompt", id: nextId++, opts, resolve });
    });
  },
  alert(opts: AlertOptions): Promise<void> {
    return new Promise<void>((resolve) => {
      push({ kind: "alert", id: nextId++, opts, resolve });
    });
  },
};

function useDialogQueue(): DialogReq | null {
  const [, setTick] = useState(0);
  useEffect(() => {
    const fn = () => setTick((t) => t + 1);
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  }, []);
  return queue[0] ?? null;
}

export function DialogRoot() {
  const head = useDialogQueue();
  if (!head) return null;

  // Esc closes the head dialog with the cancel value. Enter confirms
  // for confirm/alert (and for prompt, when the input isn't multiline).
  // Bound at the document level because the modal is in a portal anchored
  // outside the regular focus tree.
  return (
    <div className="dlg-backdrop" onMouseDown={(e) => {
      if (e.target === e.currentTarget) {
        // Click on the backdrop outside the dialog box: treat as cancel.
        if (head.kind === "confirm") resolveHead((h) => (h as ConfirmReq).resolve(false));
        else if (head.kind === "prompt") resolveHead((h) => (h as PromptReq).resolve(null));
        else resolveHead((h) => (h as AlertReq).resolve());
      }
    }}>
      {head.kind === "confirm" && <ConfirmDialog req={head} />}
      {head.kind === "prompt" && <PromptDialog req={head} />}
      {head.kind === "alert" && <AlertDialog req={head} />}
    </div>
  );
}

function ConfirmDialog({ req }: { req: ConfirmReq }) {
  const okBtnRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { okBtnRef.current?.focus(); }, []);
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); resolveHead((h) => (h as ConfirmReq).resolve(false)); }
    if (e.key === "Enter")  { e.preventDefault(); resolveHead((h) => (h as ConfirmReq).resolve(true)); }
  };
  return (
    <div className="dlg-dialog" role="alertdialog" aria-modal="true" onKeyDown={onKey}>
      <div className="dlg-header">
        <h2>{req.opts.title ?? "Confirm"}</h2>
      </div>
      <div className="dlg-body">
        <p className="dlg-message">{req.opts.message}</p>
      </div>
      <div className="dlg-footer">
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => resolveHead((h) => (h as ConfirmReq).resolve(false))}
        >{req.opts.cancelLabel ?? "Cancel"}</button>
        <button
          ref={okBtnRef}
          type="button"
          className={`btn ${req.opts.danger ? "btn-danger" : "btn-primary"}`}
          onClick={() => resolveHead((h) => (h as ConfirmReq).resolve(true))}
        >{req.opts.confirmLabel ?? (req.opts.danger ? "Delete" : "OK")}</button>
      </div>
    </div>
  );
}

function PromptDialog({ req }: { req: PromptReq }) {
  const [value, setValue] = useState<string>(req.opts.defaultValue ?? "");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    // Select the stem (before the last "."), so users renaming foo.py
    // can start typing without first nuking the extension.
    const dot = el.value.lastIndexOf(".");
    if (dot > 0) el.setSelectionRange(0, dot);
    else el.select();
  }, []);
  const submit = () => {
    const v = value;
    if (req.opts.validate) {
      const msg = req.opts.validate(v);
      if (msg) { setError(msg); return; }
    }
    resolveHead((h) => (h as PromptReq).resolve(v));
  };
  const cancel = () => resolveHead((h) => (h as PromptReq).resolve(null));
  return (
    <div className="dlg-dialog" role="dialog" aria-modal="true"
      onKeyDown={(e) => {
        if (e.key === "Escape") { e.preventDefault(); cancel(); }
        if (e.key === "Enter")  { e.preventDefault(); submit(); }
      }}
    >
      <div className="dlg-header">
        <h2>{req.opts.title ?? "Enter a value"}</h2>
      </div>
      <div className="dlg-body">
        {req.opts.message && <p className="dlg-message">{req.opts.message}</p>}
        <input
          ref={inputRef}
          className="dlg-input"
          type="text"
          value={value}
          placeholder={req.opts.placeholder}
          onChange={(e) => { setValue(e.target.value); if (error) setError(null); }}
        />
        {error && <div className="dlg-error">{error}</div>}
      </div>
      <div className="dlg-footer">
        <button type="button" className="btn btn-secondary" onClick={cancel}>
          {req.opts.cancelLabel ?? "Cancel"}
        </button>
        <button type="button" className="btn btn-primary" onClick={submit}>
          {req.opts.confirmLabel ?? "OK"}
        </button>
      </div>
    </div>
  );
}

function AlertDialog({ req }: { req: AlertReq }) {
  const okBtnRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { okBtnRef.current?.focus(); }, []);
  const close = () => resolveHead((h) => (h as AlertReq).resolve());
  const variantClass = req.opts.variant ? `dlg-variant-${req.opts.variant}` : "";
  return (
    <div className={`dlg-dialog ${variantClass}`} role="alertdialog" aria-modal="true"
      onKeyDown={(e) => {
        if (e.key === "Escape" || e.key === "Enter") { e.preventDefault(); close(); }
      }}
    >
      <div className="dlg-header">
        <h2>{req.opts.title ?? (req.opts.variant === "error" ? "Error" : "Notice")}</h2>
      </div>
      <div className="dlg-body">
        <p className="dlg-message">{req.opts.message}</p>
      </div>
      <div className="dlg-footer">
        <button ref={okBtnRef} type="button" className="btn btn-primary" onClick={close}>
          {req.opts.okLabel ?? "OK"}
        </button>
      </div>
    </div>
  );
}
