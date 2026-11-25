import { splitHighlights } from "@/lib/format";

/** Text with server-given highlight ranges. Rendered as React text nodes, so the text is always escaped. */
export function Highlighted({ text, ranges }: { text: string; ranges: number[][] }) {
  return (
    <>
      {splitHighlights(text, ranges).map((s, i) =>
        s.mark ? <mark key={i} className="rounded bg-yellow-200 px-0.5 text-slate-900">{s.text}</mark> : <span key={i}>{s.text}</span>)}
    </>
  );
}
