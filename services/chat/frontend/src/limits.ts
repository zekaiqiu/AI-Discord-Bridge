/**
 * Message-size limits, shared by the Composer (paste handling) and App
 * (send-time overflow handling).
 *
 * Why this exists: the backend caps a single message's text at
 * ``CHAT_MAX_MESSAGE_BYTES`` (app.py, default 64 KiB) and answers 413 for
 * anything larger. Before this module the SPA had no idea that ceiling
 * existed, so a big paste produced a POST that was rejected outright — and
 * because ``runTurn``'s error path assumed a worker had been created, the
 * failure showed up as an assistant bubble stuck on "streaming" forever.
 * From the user's seat the chat box just silently swallowed the message.
 *
 * The fix is to never send oversized text in the first place: an oversized
 * paste becomes a ``.txt`` attachment (which the model reads with the Read
 * tool, and which is capped at 10 MB instead of 64 KiB), and any text that
 * still exceeds the cap at send time is spilled into an attachment too.
 *
 * The real limit is read from ``/api/me`` at boot so a deployment that
 * overrides ``CHAT_MAX_MESSAGE_BYTES`` doesn't need a frontend rebuild;
 * the default below is only the pre-boot fallback.
 */

/** Mirrors app.py's ``MAX_MESSAGE_TEXT_BYTES`` default. */
export const DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024;

/** Mirrors attachments.py's ``MAX_FILE_BYTES`` / ``MAX_FILES_PER_TURN``. */
export const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
export const MAX_FILES_PER_TURN = 5;

/**
 * Fraction of the server cap we allow plain text to occupy. The headroom
 * absorbs anything appended to the user's text after this check — today
 * that's the web-search preamble in ``runTurn`` — so a message that passed
 * the client-side check can't still 413.
 */
const TEXT_BUDGET_RATIO = 0.9;

let _maxMessageBytes = DEFAULT_MAX_MESSAGE_BYTES;

/** Adopt the server's real cap (from ``/api/me``). Ignores junk values. */
export function setMaxMessageBytes(bytes: unknown): void {
  if (typeof bytes === "number" && Number.isFinite(bytes) && bytes >= 1024) {
    _maxMessageBytes = Math.floor(bytes);
  }
}

export function maxMessageBytes(): number {
  return _maxMessageBytes;
}

/** Largest text payload we're willing to POST as message text. */
export function textBudgetBytes(): number {
  return Math.floor(_maxMessageBytes * TEXT_BUDGET_RATIO);
}

export function utf8Bytes(s: string): number {
  return new TextEncoder().encode(s).length;
}

/**
 * True if ``existing + incoming`` would blow the text budget.
 *
 * Both shortcuts are exact, not heuristics, and they exist so a pathological
 * paste (tens of MB) is rejected on a cheap length compare instead of being
 * copied into a same-sized Uint8Array by TextEncoder:
 *   - UTF-8 never uses fewer bytes than UTF-16 code units, so
 *     ``length > budget`` already proves the byte count exceeds it.
 *   - UTF-8 never uses more than 4 bytes per code unit (a 2-unit surrogate
 *     pair encodes to 4 bytes), so ``length * 4 <= budget`` proves it fits.
 */
export function exceedsTextBudget(existing: string, incoming = ""): boolean {
  const budget = textBudgetBytes();
  const units = existing.length + incoming.length;
  if (units > budget) return true;
  if (units * 4 <= budget) return false;
  return utf8Bytes(existing) + utf8Bytes(incoming) > budget;
}

/** Wrap a string as a ``text/plain`` File suitable for ``onAttach``. */
export function makeTextFile(text: string, filename: string): File {
  return new File([text], filename, {
    type: "text/plain",
    lastModified: Date.now(),
  });
}
