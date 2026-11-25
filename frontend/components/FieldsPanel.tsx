import { draftValue, itemKey, type Draft, type RowMark } from "@/lib/edits";
import { confidencePct, isLowConfidence } from "@/lib/format";
import { FIELD_NAMES, ITEM_FIELDS, type FieldName, type Invoice, type ItemField } from "@/lib/types";
import { Pill, cx } from "./ui";

const LABELS: Record<FieldName, string> = {
  invoice_number: "Invoice number", invoice_date: "Invoice date", supplier_name: "Supplier", customer_name: "Customer",
  subtotal: "Subtotal", tax: "Tax", total: "Total", currency: "Currency",
};
const ITEM_LABELS: Record<ItemField, string> = { description: "Description", quantity: "Qty", unit_price: "Unit price", line_total: "Line total" };
const NUMERIC = new Set<string>(["subtotal", "tax", "total", "quantity", "unit_price", "line_total"]);

function Confidence({ value, confidence, manual }: { value: string | null; confidence?: number | null; manual?: boolean }) {
  if (manual) return <Pill tone="blue">edited by hand</Pill>;
  if (value === null) return <span className="text-xs text-slate-400">not found</span>;
  if (confidence === null || confidence === undefined) return null;
  const low = isLowConfidence(confidence);
  return (
    <span className={cx("inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium tabular-nums",
      low ? "bg-yellow-100 text-yellow-900 ring-1 ring-yellow-300" : "bg-slate-100 text-slate-700")}
      title={low ? "Low confidence: please check this value" : "Confidence"}>
      {low && <span aria-hidden="true">⚠</span>}
      <span className="sr-only">{low ? "Low confidence " : "Confidence "}</span>{confidencePct(confidence)}%
    </span>
  );
}

function RowNote({ mark }: { mark?: RowMark }) {
  if (!mark) return null;
  return <p className={cx("mt-1 text-xs", mark.severity === "error" ? "text-red-700" : "text-amber-800")}>{mark.messages.join(" · ")}</p>;
}

const rowStyle = (mark?: RowMark, flash?: boolean) =>
  cx("border-l-4 px-3 py-2 transition-colors", flash && "bg-blue-100",
    mark ? (mark.severity === "error" ? "border-red-500" : "border-amber-400") : "border-transparent");

interface Props {
  invoice: Invoice;
  marks: Record<string, RowMark>;
  editing: boolean;
  canEdit: boolean;
  draft: Draft;
  problems: Record<string, string>;
  flash: string | null;
  focusField: string | null;
  onEdit: (focus?: string) => void;
  onField: (name: FieldName, value: string) => void;
  onItem: (position: number, field: ItemField, value: string) => void;
}

export function FieldsPanel({ invoice, marks, editing, canEdit, draft, problems, flash, focusField, onEdit, onField, onItem }: Props) {
  return (
    <section className="card p-0" aria-labelledby="fields-title">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3">
        <h2 id="fields-title" className="text-sm font-semibold uppercase tracking-wide text-slate-500">Extracted fields</h2>
        {invoice.source === "spreadsheet_mapping" && <Pill>mapped from columns</Pill>}
        {invoice.is_manually_corrected && <Pill tone="blue">corrected by hand</Pill>}
        {invoice.pages[0] != null && <Pill>{invoice.pages[0] === invoice.pages[1] ? `page ${invoice.pages[0]}` : `pages ${invoice.pages[0]}–${invoice.pages[1]}`}</Pill>}
        {canEdit && !editing && <span className="ml-auto text-xs text-slate-400">Click a value to edit</span>}
      </div>

      <dl className="divide-y divide-slate-100">
        {FIELD_NAMES.map((name) => {
          const f = invoice.fields[name];
          const id = `field-${name}`;
          const mark = marks[id];
          const low = !editing && f.value !== null && isLowConfidence(f.confidence) && !f.manual;
          const err = problems[name];
          return (
            <div key={name} id={id} className={cx(rowStyle(mark, flash === id), low && !mark && "bg-yellow-50",
              "grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[9rem_1fr_auto]")}>
              <dt className="pt-1.5 text-sm text-slate-500">{LABELS[name]}</dt>
              <dd className="min-w-0">
                {editing ? (
                  <input className={cx("input", err && "border-red-500")} value={draftValue(draft, name, f)} autoFocus={focusField === name}
                    type={name === "invoice_date" ? "date" : "text"} inputMode={NUMERIC.has(name) ? "decimal" : undefined}
                    aria-label={LABELS[name]} aria-invalid={!!err} onChange={(e) => onField(name, e.target.value)} />
                ) : (
                  <div className={cx("rounded px-1 py-1.5 text-sm", canEdit && "cursor-text hover:bg-slate-100", NUMERIC.has(name) && "tabular-nums")}
                    onClick={canEdit ? () => onEdit(name) : undefined} role={canEdit ? "button" : undefined} tabIndex={canEdit ? 0 : undefined}
                    onKeyDown={canEdit ? (e) => { if (e.key === "Enter") onEdit(name); } : undefined}
                    title={canEdit ? "Click to edit" : undefined}>
                    {f.value ?? <span className="text-slate-400">—</span>}
                  </div>
                )}
                {err && <p className="mt-1 text-xs text-red-700" role="alert">{err}</p>}
                {!editing && f.source_snippet && f.value !== null && (
                  <p className="mt-0.5 truncate text-xs text-slate-400" title={`From the document: ${f.source_snippet}`}>“{f.source_snippet}”</p>
                )}
                <RowNote mark={mark} />
              </dd>
              <dd className="pt-1.5"><Confidence value={f.value} confidence={f.confidence} manual={f.manual} /></dd>
            </div>
          );
        })}
      </dl>

      <div className="border-t border-slate-100 px-4 py-3">
        <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">Line items</h3>
        {invoice.line_items.length === 0 ? (
          <p className="text-sm text-slate-500">No line items were found in this document.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[34rem] text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-slate-500">
                <tr><th className="py-1 pr-2">#</th>{ITEM_FIELDS.map((f) => <th key={f} className={cx("py-1 pr-2", f !== "description" && "text-right")}>{ITEM_LABELS[f]}</th>)}</tr>
              </thead>
              <tbody>
                {invoice.line_items.map((it) => {
                  const id = `li-${it.position}`;
                  const mark = marks[id];
                  return (
                    <tr key={it.position} id={id} className={cx("border-t border-slate-100 align-top transition-colors", flash === id && "bg-blue-100",
                      mark && (mark.severity === "error" ? "bg-red-50" : "bg-amber-50"))}>
                      <td className="py-1.5 pr-2 text-slate-400">{it.position}</td>
                      {ITEM_FIELDS.map((f) => {
                        const key = itemKey(it.position, f);
                        const err = problems[key];
                        const shown = key in draft.items ? draft.items[key] : it[f] ?? "";
                        return (
                          <td key={f} className={cx("py-1.5 pr-2", f !== "description" && "text-right tabular-nums")}>
                            {editing ? (
                              <input className={cx("input px-1.5 py-1", f !== "description" && "text-right", err && "border-red-500")} value={shown}
                                aria-label={`${ITEM_LABELS[f]} of line ${it.position}`} aria-invalid={!!err} inputMode={f === "description" ? undefined : "decimal"}
                                onChange={(e) => onItem(it.position, f, e.target.value)} />
                            ) : (it[f] ?? <span className="text-slate-400">—</span>)}
                            {err && <p className="text-xs text-red-700" role="alert">{err}</p>}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {Object.keys(marks).some((k) => k.startsWith("li-")) && (
              <ul className="mt-2 space-y-0.5 text-xs">
                {Object.entries(marks).filter(([k]) => k.startsWith("li-")).map(([k, m]) => (
                  <li key={k} className={m.severity === "error" ? "text-red-700" : "text-amber-800"}>Line {k.slice(3)}: {m.messages.join(" · ")}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
