import { findingTarget } from "@/lib/edits";
import type { Finding, Invoice } from "@/lib/types";
import { Pill, cx } from "./ui";

/** Errors first, then warnings. Each finding says which field it is about and jumps to it when clicked. */
export function ValidationPanel({ findings, invoices, status, onJump }: {
  findings: Finding[]; invoices: Invoice[]; status: string; onJump: (rowId: string) => void;
}) {
  const open = findings.filter((f) => !f.resolved);
  const errors = open.filter((f) => f.severity === "error");
  const warnings = open.filter((f) => f.severity === "warning");
  const sorted = [...errors, ...warnings, ...findings.filter((f) => f.resolved)];
  const invoiceOf = (f: Finding) => invoices.find((i) => i.id === f.result_id)?.invoice_index;

  return (
    <section className="card" aria-labelledby="validation-title">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 id="validation-title" className="text-sm font-semibold uppercase tracking-wide text-slate-500">Validation</h2>
        {errors.length > 0 && <Pill tone="red">{errors.length} error{errors.length > 1 ? "s" : ""}</Pill>}
        {warnings.length > 0 && <Pill tone="amber">{warnings.length} warning{warnings.length > 1 ? "s" : ""}</Pill>}
      </div>
      {sorted.length === 0 ? (
        status === "completed"
          ? <p className="rounded-md bg-green-50 px-3 py-2 text-sm text-green-800">All checks passed. Nothing to review.</p>
          : <p className="text-sm text-slate-500">No findings yet.</p>
      ) : (
        <ul className="space-y-2">
          {sorted.map((f, i) => {
            const target = findingTarget(f.field);
            const inv = invoiceOf(f);
            return (
              <li key={i} className={cx("flex flex-col gap-1 rounded-md border-l-4 px-3 py-2 text-sm sm:flex-row sm:items-start sm:justify-between sm:gap-3",
                f.resolved ? "border-slate-300 bg-slate-50 text-slate-500 line-through"
                  : f.severity === "error" ? "border-red-500 bg-red-50 text-red-900" : "border-amber-400 bg-amber-50 text-amber-900")}>
                <div className="min-w-0">
                  <span className="mr-2 font-mono text-xs font-semibold">{f.rule_code}</span>
                  <span className="mr-1 text-xs uppercase opacity-70">{f.severity}{f.resolved ? " · resolved" : ""}</span>
                  <p className="mt-0.5">{f.message}</p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                  {invoices.length > 1 && inv !== undefined && <Pill>Invoice {inv + 1}</Pill>}
                  {f.field && (target
                    ? <button className="rounded bg-white/70 px-1.5 py-0.5 font-mono text-xs ring-1 ring-current hover:bg-white" onClick={() => onJump(target)} title="Go to this field">{f.field}</button>
                    : <span className="rounded bg-white/70 px-1.5 py-0.5 font-mono text-xs ring-1 ring-current">{f.field}</span>)}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
