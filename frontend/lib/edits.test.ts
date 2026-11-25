import { describe, expect, it } from "vitest";
import { buildCorrection, draftValue, emptyDraft, findingTarget, itemKey, markRows, validateDraft } from "./edits";
import type { Finding, Invoice } from "./types";

const field = (value: string | null, confidence = 0.9) => ({ value, confidence, source_snippet: null });
const invoice = (): Invoice => ({
  id: "r1", invoice_index: 0, pages: [1, 1], version: 1, source: null, is_manually_corrected: false,
  fields: {
    invoice_number: field("INV-1"), invoice_date: field("2026-01-05"), supplier_name: field("Acme"), customer_name: field(null),
    subtotal: field("100.00"), tax: field("5.00"), total: field("105.00"), currency: field("USD"),
  },
  line_items: [
    { position: 1, description: "A", quantity: "2.000", unit_price: "30.00", line_total: "60.00" },
    { position: 2, description: "B", quantity: "1.000", unit_price: "40.00", line_total: "40.00" },
  ],
});

describe("buildCorrection", () => {
  it("returns null when nothing changed", () => {
    expect(buildCorrection(invoice(), emptyDraft())).toBeNull();
    expect(buildCorrection(invoice(), { fields: { total: "105.00", supplier_name: "Acme " }, items: {} })).toBeNull(); // same after trimming
  });
  it("treats 105, 105.0 and 105.00 as the same amount", () => {
    expect(buildCorrection(invoice(), { fields: { total: "105" }, items: {} })).toBeNull();
    expect(buildCorrection(invoice(), { fields: { total: "105.0" }, items: {} })).toBeNull();
    expect(buildCorrection(invoice(), { fields: { total: "105.01" }, items: {} })).not.toBeNull();
  });
  it("sends only what changed", () => {
    const body = buildCorrection(invoice(), { fields: { total: "110.00", tax: "5.00", customer_name: "Carol" }, items: {} }, "alice");
    expect(body).toEqual({ invoice_index: 0, fields: { total: "110.00", customer_name: "Carol" }, line_items: [], corrected_by: "alice" });
  });
  it("sends null to clear a value, and nothing for an empty box that was already empty", () => {
    expect(buildCorrection(invoice(), { fields: { supplier_name: "  " }, items: {} })?.fields).toEqual({ supplier_name: null });
    expect(buildCorrection(invoice(), { fields: { customer_name: "" }, items: {} })).toBeNull();
  });
  it("groups line item edits by position and skips untouched cells", () => {
    const d = { fields: {}, items: { [itemKey(1, "line_total")]: "61.00", [itemKey(1, "quantity")]: "2", [itemKey(2, "description")]: "B2" } };
    expect(buildCorrection(invoice(), d)?.line_items).toEqual([{ position: 1, line_total: "61.00" }, { position: 2, description: "B2" }]);
  });
  it("addresses the invoice it was built for", () => {
    const inv = { ...invoice(), invoice_index: 3 };
    expect(buildCorrection(inv, { fields: { tax: "6" }, items: {} })?.invoice_index).toBe(3);
  });
});

describe("validateDraft", () => {
  const v = (fields: Record<string, string>, items: Record<string, string> = {}) => validateDraft({ fields, items });
  it("accepts good values and empty ones", () => {
    expect(v({ total: "1250.50", invoice_date: "2026-02-28", currency: "usd", invoice_number: "X-1", subtotal: "" })).toEqual({});
  });
  it("catches bad money", () => {
    for (const bad of ["abc", "1,250.50", "12.345", "1e5", "--3", "$5"]) expect(v({ total: bad }).total).toBeTruthy();
  });
  it("catches bad dates, including real-looking impossible ones", () => {
    for (const bad of ["03/04/2026", "2026-13-01", "2026-02-30", "2026-2-3", "tomorrow"]) expect(v({ invoice_date: bad }).invoice_date).toBeTruthy();
    expect(v({ invoice_date: "2024-02-29" })).toEqual({}); // leap day
  });
  it("catches bad currency codes", () => {
    for (const bad of ["US", "DOLLAR", "U$D", "12A"]) expect(v({ currency: bad }).currency).toBeTruthy();
  });
  it("allows 3 decimals for quantity but only 2 for prices", () => {
    expect(v({}, { "1.quantity": "2.125" })).toEqual({});
    expect(v({}, { "1.quantity": "2.1255" })["1.quantity"]).toBeTruthy();
    expect(v({}, { "1.unit_price": "2.125" })["1.unit_price"]).toBeTruthy();
  });
  it("limits text length", () => expect(v({ invoice_number: "x".repeat(129) }).invoice_number).toBeTruthy());
});

describe("draftValue", () => {
  it("shows the draft once the user has typed, even an empty string", () => {
    const f = field("Acme");
    expect(draftValue(emptyDraft(), "supplier_name", f)).toBe("Acme");
    expect(draftValue({ fields: { supplier_name: "" }, items: {} }, "supplier_name", f)).toBe("");
  });
});

describe("findings to rows", () => {
  it("maps fields and line items to the ids used in the page", () => {
    expect(findingTarget("total")).toBe("field-total");
    expect(findingTarget("line_items[2].line_total")).toBe("li-2");
    expect(findingTarget("line_items[10]")).toBe("li-10");
    expect(findingTarget(null)).toBeNull();
    expect(findingTarget("line_items.description")).toBeNull();
    expect(findingTarget("nonsense")).toBeNull();
  });
  const f = (field: string | null, severity: "error" | "warning", extra: Partial<Finding> = {}): Finding =>
    ({ rule_code: "R", severity, field, message: `${field}-${severity}`, resolved: false, result_id: "r1", ...extra });
  it("marks a row red if any error is on it, collecting all messages", () => {
    const m = markRows([f("total", "warning"), f("total", "error"), f("tax", "warning")]);
    expect(m["field-total"]).toEqual({ severity: "error", messages: ["total-warning", "total-error"] });
    expect(m["field-tax"].severity).toBe("warning");
  });
  it("ignores resolved findings, findings of other invoices, and ones with no field", () => {
    const m = markRows([f("total", "error", { resolved: true }), f("tax", "error", { result_id: "other" }), f(null, "error")], "r1");
    expect(m).toEqual({});
  });
});
