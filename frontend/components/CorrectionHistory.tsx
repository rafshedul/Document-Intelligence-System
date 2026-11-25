import { formatDateTime } from "@/lib/format";
import type { CorrectionEntry } from "@/lib/types";
import { ErrorState, LoadingBlock } from "./ui";

export function CorrectionHistory({ entries, loading, error, multi, onRetry }: {
  entries: CorrectionEntry[] | null; loading: boolean; error: unknown; multi: boolean; onRetry: () => void;
}) {
  return (
    <details className="card" open={!!entries?.length}>
      <summary className="cursor-pointer text-sm font-semibold uppercase tracking-wide text-slate-500">
        Correction history{entries ? ` (${entries.length})` : ""}
      </summary>
      <div className="mt-3">
        {loading && !entries && <LoadingBlock label="Loading history…" />}
        {error != null && <ErrorState error={error} onRetry={onRetry} title="Could not load the history" />}
        {entries && entries.length === 0 && <p className="text-sm text-slate-500">No manual corrections yet. Every change you make is logged here.</p>}
        {entries && entries.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[32rem] text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-slate-500">
                <tr><th className="py-1 pr-3">When</th><th className="py-1 pr-3">Field</th><th className="py-1 pr-3">Was</th><th className="py-1 pr-3">Now</th><th className="py-1">By</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {entries.map((c) => (
                  <tr key={c.id} className="align-top">
                    <td className="whitespace-nowrap py-1.5 pr-3 text-slate-600">{formatDateTime(c.corrected_at)}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs">{multi ? `#${c.invoice_index + 1} ` : ""}{c.field}</td>
                    <td className="py-1.5 pr-3 text-red-700 line-through decoration-red-300">{c.old_value ?? <span className="italic text-slate-400">empty</span>}</td>
                    <td className="py-1.5 pr-3 text-green-700">{c.new_value ?? <span className="italic text-slate-400">empty</span>}</td>
                    <td className="py-1.5 text-slate-600">{c.corrected_by ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </details>
  );
}
