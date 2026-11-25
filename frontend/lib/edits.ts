import type { CorrectionRequest, FieldName, FieldValue, Finding, Invoice, ItemField } from "./types";
import { FIELD_NAMES, ITEM_FIELDS } from "./types";

/** What the user typed. Item keys look like "2.line_total". */
export interface Draft { fields: Partial<Record<FieldName, string>>; items: Record<string, string> }
export const emptyDraft = (): Draft => ({ fields: {}, items: {} });
export const itemKey = (position: number, field: ItemField): string => `${position}.${field}`;

const norm = (v: string | null | undefined): string | null => {
  const t = (v ?? "").trim();
  return t === "" ? null : t;
};
// "100", "100.0" and "100.00" are the same amount
const sameValue = (a: string | null, b: string | null): boolean => {
  if (a === b) return true;
  if (a !== null && b !== null && /^-?\d+(\.\d+)?$/.test(a) && /^-?\d+(\.\d+)?$/.test(b)) return Number(a) === Number(b) && a.replace(/\.?0+$/, "") === b.replace(/\.?0+$/, "");
  return false;
};

/** Only what changed, in the PATCH body shape. Returns null when nothing changed. */
export function buildCorrection(invoice: Invoice, draft: Draft, correctedBy = "web"): CorrectionRequest | null {
  const fields: Partial<Record<FieldName, string | null>> = {};
  for (const name of FIELD_NAMES) {
    if (!(name in draft.fields)) continue;
    const next = norm(draft.fields[name]);
    if (!sameValue(next, norm(invoice.fields[name].value))) fields[name] = next;
  }
  const items: NonNullable<CorrectionRequest["line_items"]> = [];
  for (const item of invoice.line_items) {
    const edit: { position: number } & Partial<Record<ItemField, string | null>> = { position: item.position };
    let changed = false;
    for (const f of ITEM_FIELDS) {
      const k = itemKey(item.position, f);
      if (!(k in draft.items)) continue;
      const next = norm(draft.items[k]);
      if (!sameValue(next, norm(item[f]))) { edit[f] = next; changed = true; }
    }
    if (changed) items.push(edit);
  }
  if (!Object.keys(fields).length && !items.length) return null;
  return { invoice_index: invoice.invoice_index, fields, line_items: items, corrected_by: correctedBy };
}

/** Same rules as the backend, so mistakes show before saving. Returns messages by key ("total", "2.quantity"). */
export function validateDraft(draft: Draft): Record<string, string> {
  const problems: Record<string, string> = {};
  const money = (key: string, raw: string | undefined, places: number) => {
    const v = norm(raw);
    if (v === null) return;
    if (!/^-?\d+(\.\d+)?$/.test(v)) problems[key] = "Enter a plain number, like 1250.50";
    else if ((v.split(".")[1] ?? "").length > places) problems[key] = `At most ${places} decimal places`;
  };
  for (const [name, raw] of Object.entries(draft.fields) as [FieldName, string][]) {
    const v = norm(raw);
    if (v === null) continue;
    if (name === "invoice_date") {
      const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
      const d = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
      if (!m || !d || d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3]) problems[name] = "Use a real date as YYYY-MM-DD";
    } else if (name === "subtotal" || name === "tax" || name === "total") money(name, raw, 2);
    else if (name === "currency") { if (!/^[A-Za-z]{3}$/.test(v)) problems[name] = "Use a 3-letter code, like USD"; }
    else if (v.length > (name === "invoice_number" ? 128 : 512)) problems[name] = "Too long";
  }
  for (const [key, raw] of Object.entries(draft.items)) {
    const f = key.split(".")[1] as ItemField;
    if (f === "quantity") money(key, raw, 3);
    else if (f === "unit_price" || f === "line_total") money(key, raw, 2);
  }
  return problems;
}

export function draftValue(draft: Draft, name: FieldName, field: FieldValue): string {
  return name in draft.fields ? (draft.fields[name] as string) : field.value ?? "";
}

// ---- which row a validation finding belongs to ---------------------------------------------------------

export interface RowMark { severity: "error" | "warning"; messages: string[] }

/** "total" -> "field-total"; "line_items[2].line_total" -> "li-2". Findings without a field map to null. */
export function findingTarget(field: string | null): string | null {
  if (!field) return null;
  const m = /^line_items\[(\d+)\]/.exec(field);
  if (m) return `li-${m[1]}`;
  return (FIELD_NAMES as string[]).includes(field) ? `field-${field}` : null;
}

export function markRows(findings: Finding[], resultId?: string): Record<string, RowMark> {
  const out: Record<string, RowMark> = {};
  for (const f of findings) {
    if (f.resolved || (resultId && f.result_id && f.result_id !== resultId)) continue;
    const id = findingTarget(f.field);
    if (!id) continue;
    const cur = out[id];
    out[id] = { severity: cur?.severity === "error" || f.severity === "error" ? "error" : "warning", messages: [...(cur?.messages ?? []), f.message] };
  }
  return out;
}
