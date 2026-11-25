export const LOW_CONFIDENCE = 0.7; // same threshold the backend's LOW_CONFIDENCE rule uses

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export const confidencePct = (c: number): number => Math.round(Math.max(0, Math.min(1, c)) * 100);
export const isLowConfidence = (c: number | null | undefined): boolean => typeof c === "number" && c < LOW_CONFIDENCE;

export const statusLabel = (s: string): string => s.replace(/_/g, " ");

export interface Segment { text: string; mark: boolean }

/** Cut `text` at highlight offsets [start, end). Offsets come from the server; bad ones are ignored. */
export function splitHighlights(text: string, ranges: number[][]): Segment[] {
  const valid = ranges
    .filter((r) => r.length === 2 && Number.isInteger(r[0]) && Number.isInteger(r[1]) && r[0] >= 0 && r[1] > r[0] && r[0] < text.length)
    .map(([a, b]) => [a, Math.min(b, text.length)] as [number, number])
    .sort((x, y) => x[0] - y[0]);
  const out: Segment[] = [];
  let at = 0;
  for (const [a, b] of valid) {
    if (a < at) continue; // overlaps the previous highlight
    if (a > at) out.push({ text: text.slice(at, a), mark: false });
    out.push({ text: text.slice(a, b), mark: true });
    at = b;
  }
  if (at < text.length) out.push({ text: text.slice(at), mark: false });
  return out.length ? out : [{ text, mark: false }];
}
