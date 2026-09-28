/**
 * Small UI helpers shared across components.
 *
 * Currently just byte-size formatting (used by the composer attachment
 * chips and the thread-bubble attachment list). Putting it here means a
 * future threshold change — say, adding a GB tier — is one edit, not two.
 *
 * Behavior is byte-identical to the previous in-file copies in
 * Composer.tsx and Thread.tsx. The largest size that can actually reach
 * this function is the per-file upload cap (10 MB, enforced by
 * services/chat/attachments.py), so the MB tier is the practical ceiling.
 */

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
