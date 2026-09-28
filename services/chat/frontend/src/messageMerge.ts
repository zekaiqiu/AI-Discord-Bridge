/**
 * Reconciliation between the live, in-browser message buffer and a server
 * snapshot of the same session.
 *
 * Extracted from App.tsx so the rules can be exercised on their own: every
 * bug this file has had was a data bug, not a rendering one, and each of
 * them was cheap to state as an input/output pair and expensive to find
 * through the UI.
 */
import { Message } from "./api";

const EMPTY_MESSAGES: Message[] = [];

// Merge a server snapshot into a local message buffer without regressing
// the trailing assistant message's content. The server snapshot is the
// session JSON the backend has persisted so far; the local buffer is
// what's been accumulated in-process from the live SSE stream (which
// usually has more text than has yet been flushed to disk). Naively
// replacing local with server causes "content disappears mid-stream"
// every time the refresh path fires (tab focus, session click, the
// recoverFromStreamError 2-second poll). See replaceMessages for the
// caller-side rationale.
//
// Returns the same reference as `local` when no change is needed, so
// callers can `if (merged === local) return prev` to skip the setState.
export function mergePreservingLiveTail(local: Message[], server: Message[]): Message[] {
  // Empty local: no risk of regression — accept server unconditionally.
  if (local.length === 0) return server;
  // Empty server: nothing to merge in. Keep local.
  if (server.length === 0) return local;

  // ---- 1. Split off the local-only (pending) tail --------------------
  // Messages the user just sent that the server has NOT acknowledged yet
  // (runTurn marks them `pending` and clears the flag the instant the POST
  // returns 2xx). These exist nowhere but this tab, so no server snapshot
  // can possibly be evidence against them: they are carried through the
  // merge verbatim and re-appended at the end.
  //
  // This replaces a POSITIONAL rule — `[...server, ...local.slice(server.length)]`
  // — that was only correct while local and server agreed message-for-message
  // on their shared head. They routinely don't: a scheduled wake appends an
  // assistant turn with no user turn in front of it, and the idle poll only
  // notices every IDLE_RESYNC_MS. Send during that window and local is one
  // BEHIND server on the head while being two AHEAD on the tail, so
  // `slice(server.length)` sheared off exactly one message from the front of
  // the local tail — the user's own bubble — leaving the placeholder to
  // stream a reply to a question that was no longer on screen.
  //
  // Invariant, enforced by runTurn: pending messages form a single run at
  // the END of the buffer (a new send clears any older pending pair first).
  // `acked` is nonetheless taken by filter rather than by slice, so a stray
  // pending entry further up can't shift the positional reasoning below it.
  let cut = local.length;
  while (cut > 0 && local[cut - 1].pending) cut -= 1;
  const pending = cut === local.length ? EMPTY_MESSAGES : local.slice(cut);
  const acked =
    cut === local.length
      ? local
      : local.slice(0, cut).filter((m) => !m.pending);

  const merged = mergeAcknowledged(acked, server);
  // Self-heal: drop the pending pair if the server turns out to have it
  // after all. A send can fail on the client side of a request the origin
  // went on to process anyway (an edge timeout closes the browser's socket
  // but does not cancel the handler), and re-appending a copy of a message
  // the snapshot already contains would turn a lost message into a
  // duplicated one. Matched on exact content within the region of the
  // snapshot we hadn't seen — the user's own text, verbatim, is a reliable
  // enough key over a window that small.
  const out =
    pending.length === 0 || serverHasPendingUser(server, acked.length, pending)
      ? merged
      : [...merged, ...pending];
  // Preserve the "same reference when nothing changed" contract that
  // replaceMessages relies on to skip the setState. mergeAcknowledged
  // almost always returns the freshly-parsed `server` array, so without
  // this a 2-second recovery poll would re-render every open Thread on
  // every tick even when the session is completely idle.
  return sameMessageList(out, local) ? local : out;
}

// Does the snapshot already contain the user message this client still
// thinks is unsent? Only the part of the snapshot beyond what we'd already
// acknowledged is considered, so re-sending identical text ("yes", "go on")
// can't make the new copy match the old one.
function serverHasPendingUser(
  server: Message[],
  ackedLength: number,
  pending: Message[],
): boolean {
  const user = pending.find((m) => m.role === "user");
  if (!user) return false;
  for (let i = Math.max(0, ackedLength); i < server.length; i += 1) {
    const m = server[i];
    if (m.role === "user" && m.content === user.content) return true;
  }
  return false;
}

// Value equality over the fields that can actually change between two
// snapshots of the same message. Reference equality is not enough: `server`
// is freshly parsed JSON on every poll, so its objects are never identical
// to the ones already in state, and a reference test would report "changed"
// on every single tick of an idle session.
function sameMessageList(a: Message[], b: Message[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (!sameMessage(a[i], b[i])) return false;
  }
  return true;
}

function sameMessage(a: Message, b: Message): boolean {
  if (a === b) return true;
  if (
    a.role !== b.role ||
    a.content !== b.content ||
    a.ts !== b.ts ||
    a.seq !== b.seq ||
    a.status !== b.status ||
    a.via !== b.via ||
    !!a.pending !== !!b.pending
  ) {
    return false;
  }
  // meta (the token/timing footer) and attachments are small and only ever
  // set once, so a structural compare on the rare reference mismatch is
  // cheaper than the re-render it avoids.
  return (
    sameJson(a.meta, b.meta) && sameJson(a.attachments, b.attachments)
  );
}

function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a == null || b == null) return a == null && b == null;
  return JSON.stringify(a) === JSON.stringify(b);
}

function mergeAcknowledged(local: Message[], server: Message[]): Message[] {
  if (local.length === 0) return server;

  // Local is ahead of a snapshot that was already in flight when the server
  // did its append (rare — every getSession caller skips while a local
  // stream is attached). Only the trailing entries the server demonstrably
  // hasn't assigned a seq to can be ahead; anything carrying a seq the
  // server already knows about is stale local state, not news.
  if (local.length > server.length) {
    const tail = local
      .slice(server.length)
      .filter((m) => typeof m.seq !== "number");
    return tail.length ? [...server, ...tail] : server;
  }

  const lastL = local[local.length - 1];
  const lastS = server[server.length - 1];

  // Only the trailing assistant message races with live deltas. If
  // either side's tail isn't an assistant message, the server snapshot
  // is fine to apply (user messages are immutable post-send).
  if (lastL.role !== "assistant" || lastS.role !== "assistant") {
    return server;
  }

  // The two tails must be the SAME message before any content merging is
  // meaningful. When the server has grown a message we've never seen —
  // exactly what a SCHEDULED WAKE does, appending an empty assistant
  // placeholder with no user turn in front of it — the tails are different
  // messages, and the content check below would graft the PREVIOUS reply's
  // text onto the wake's empty placeholder. The wake bubble then rendered
  // the last answer over again and streamed its real reply onto the end of
  // it. Compare seq when both sides have one; fall back to equal lengths for
  // the optimistic placeholder, which has no seq until the server assigns it.
  const sameTail =
    typeof lastL.seq === "number" && typeof lastS.seq === "number"
      ? lastL.seq === lastS.seq
      : local.length === server.length;
  if (!sameTail) return server;

  // The merge condition: if the local copy has MORE content than the
  // server snapshot for the same trailing slot, the snapshot is stale
  // and would visibly truncate the stream. Keep local's content but
  // adopt server's other fields (id, status, model, timestamps) so any
  // server-side finalization (e.g. status flip to "complete") still
  // propagates. If server has equal or more content, server wins
  // (terminal "done" event has already delivered full_text locally,
  // or the snapshot has overtaken us — either way no regression).
  const localContent = lastL.content || "";
  const serverContent = lastS.content || "";
  if (localContent.length > serverContent.length) {
    const mergedTail: Message = { ...lastS, content: localContent };
    return [...server.slice(0, -1), mergedTail];
  }
  return server;
}
