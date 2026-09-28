/**
 * Rules check for src/messageMerge.ts. No test runner is wired into this
 * frontend, so this is a plain script:
 *
 *   npm run check:merge
 *
 * Each case is a bug that reached production at least once. The first two
 * are the "my message disappeared while the AI was replying" report: the
 * local buffer trails the server by a message or two (a scheduled wake, a
 * send from another device) at the moment the user hits send, and the old
 * positional merge sheared exactly that many messages off the front of the
 * local tail — starting with the user's own bubble.
 */
import { mergePreservingLiveTail as merge } from "../src/messageMerge";
import type { Message } from "../src/api";

let failures = 0;
function check(name: string, got: unknown, want: unknown): void {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { failures++; console.log(`FAIL ${name}\n  got  ${a}\n  want ${b}`); }
  else console.log(`ok   ${name}`);
}
const u = (seq: number | undefined, c: string, extra: Partial<Message> = {}): Message =>
  ({ role: "user", content: c, ts: "t", ...(seq === undefined ? {} : { seq }), ...extra });
const a = (seq: number | undefined, c: string, extra: Partial<Message> = {}): Message =>
  ({ role: "assistant", content: c, ts: "t", ...(seq === undefined ? {} : { seq }), ...extra });
const txt = (ms: Message[]) => ms.map((m) => `${m.role[0]}:${m.content}`).join("|");

// --- THE REPORTED BUG -----------------------------------------------------
// A wake fired and finished; the idle poll hasn't picked it up yet, so local
// is one BEHIND server on the head. The user sends while that's true.
{
  const server = [u(0, "hi"), a(1, "hello"), a(2, "wake reply", { via: "wake" })];
  const local  = [u(0, "hi"), a(1, "hello"),
                  u(undefined, "my new question", { pending: true }),
                  a(undefined, "", { status: "streaming", pending: true })];
  check("wake-offset: user message survives",
    txt(merge(local, server)),
    "u:hi|a:hello|a:wake reply|u:my new question|a:");
}
// Two messages behind (wake + its user turn from another tab).
{
  const server = [u(0, "hi"), a(1, "hello"), u(2, "from phone"), a(3, "phone reply")];
  const local  = [u(0, "hi"), a(1, "hello"),
                  u(undefined, "desktop msg", { pending: true }),
                  a(undefined, "", { pending: true })];
  check("two-behind: user message survives",
    txt(merge(local, server)),
    "u:hi|a:hello|u:from phone|a:phone reply|u:desktop msg|a:");
}
// Snapshot longer than local, no pending: server wins outright.
{
  const server = [u(0, "hi"), a(1, "hello"), a(2, "wake")];
  const local  = [u(0, "hi"), a(1, "hello")];
  check("no pending: server authoritative", txt(merge(local, server)), "u:hi|a:hello|a:wake");
}
// --- REGRESSIONS THE OLD CODE GOT RIGHT, WHICH MUST STAY RIGHT ------------
// Live tail: local has streamed more text than the snapshot has flushed.
{
  const server = [u(0, "hi"), a(1, "partial")];
  const local  = [u(0, "hi"), a(1, "partial and then some more")];
  check("live tail not truncated", txt(merge(local, server)), "u:hi|a:partial and then some more");
}
// Wake placeholder must not inherit the previous reply's text.
{
  const server = [u(0, "hi"), a(1, "old answer"), a(2, "", { status: "streaming", via: "wake" })];
  const local  = [u(0, "hi"), a(1, "old answer")];
  check("wake placeholder stays empty", txt(merge(local, server)), "u:hi|a:old answer|a:");
}
// Snapshot in flight before the append: optimistic pair is ahead.
{
  const server = [u(0, "hi"), a(1, "hello")];
  const local  = [u(0, "hi"), a(1, "hello"),
                  u(undefined, "q", { pending: true }), a(undefined, "", { pending: true })];
  check("stale snapshot: pair survives", txt(merge(local, server)), "u:hi|a:hello|u:q|a:");
}
// Accepted (pending cleared) + snapshot now has both: no duplication.
{
  const server = [u(0, "hi"), a(1, "hello"), u(2, "q"), a(3, "answer")];
  const local  = [u(0, "hi"), a(1, "hello"), u(undefined, "q"), a(undefined, "answer")];
  check("accepted pair: no duplicate", txt(merge(local, server)), "u:hi|a:hello|u:q|a:answer");
}
// Self-heal: send failed client-side but the origin processed it anyway.
{
  const server = [u(0, "hi"), a(1, "hello"), u(2, "q"), a(3, "answer")];
  const local  = [u(0, "hi"), a(1, "hello"),
                  u(undefined, "q", { pending: true }), a(undefined, "", { pending: true })];
  check("self-heal: no duplicate bubble", txt(merge(local, server)), "u:hi|a:hello|u:q|a:answer");
}
// Identical repeated text must not self-heal against the OLD copy.
{
  const server = [u(0, "yes"), a(1, "ok")];
  const local  = [u(0, "yes"), a(1, "ok"),
                  u(undefined, "yes", { pending: true }), a(undefined, "", { pending: true })];
  check("repeat text: pending kept", txt(merge(local, server)), "u:yes|a:ok|u:yes|a:");
}
// A failed send's pair must survive later snapshots indefinitely.
{
  const server = [u(0, "hi"), a(1, "hello"), a(2, "wake1"), a(3, "wake2")];
  const local  = [u(0, "hi"), a(1, "hello"),
                  u(undefined, "lost msg", { pending: true }),
                  a(undefined, "not sent", { status: "error", pending: true })];
  check("failed send survives growth",
    txt(merge(local, server)), "u:hi|a:hello|a:wake1|a:wake2|u:lost msg|a:not sent");
}
// No-op merges keep the same array reference (skips the setState).
{
  const local = [u(0, "hi"), a(1, "hello")];
  const server = [u(0, "hi"), a(1, "hello")];
  check("no-op identity", merge(local, server) === local, true);
}
// Empty cases.
check("empty local", txt(merge([], [u(0, "x")])), "u:x");
check("empty server", txt(merge([u(undefined, "x", { pending: true })], [])), "u:x");

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
if (failures > 0) throw new Error(`${failures} merge rule failure(s)`);
