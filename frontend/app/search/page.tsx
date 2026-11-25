"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Highlighted } from "@/components/Highlighted";
import { EmptyState, ErrorState, LoadingBlock, Pagination, Pill, StatusBadge, cx } from "@/components/ui";
import { ApiError, api, query } from "@/lib/api";
import { statusLabel } from "@/lib/format";
import { DOC_STATUSES, type SearchMode, type SearchResponse } from "@/lib/types";

const MODES: { id: SearchMode; label: string; hint: string }[] = [
  { id: "hybrid", label: "Hybrid", hint: "Both methods together (recommended)" },
  { id: "keyword", label: "Keyword", hint: "Exact words; supports \"a phrase\", OR, -exclude" },
  { id: "semantic", label: "Semantic", hint: "Similar meaning (needs an embedding model)" },
];
const isMode = (s: string | null): s is SearchMode => s === "keyword" || s === "semantic" || s === "hybrid";

function SearchView() {
  const router = useRouter();
  const sp = useSearchParams();
  // the URL is the source of truth, so results survive a refresh and the back button
  const q = sp.get("q") ?? "";
  const mode: SearchMode = isMode(sp.get("mode")) ? (sp.get("mode") as SearchMode) : "hybrid";
  const supplier = sp.get("supplier") ?? "";
  const dateFrom = sp.get("date_from") ?? "";
  const dateTo = sp.get("date_to") ?? "";
  const status = sp.get("status") ?? "";
  const page = Math.max(1, Number(sp.get("page")) || 1);

  const [form, setForm] = useState({ q, mode, supplier, date_from: dateFrom, date_to: dateTo, status });
  const [data, setData] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => setForm({ q, mode, supplier, date_from: dateFrom, date_to: dateTo, status }), [q, mode, supplier, dateFrom, dateTo, status]);

  useEffect(() => {
    if (!q.trim()) { setData(null); setError(null); return; }
    const ctrl = new AbortController();
    setLoading(true); setError(null);
    api.search({ q, mode, supplier: supplier || undefined, date_from: dateFrom || undefined, date_to: dateTo || undefined,
      status: status || undefined, page, page_size: 10 }, ctrl.signal)
      .then(setData)
      .catch((e) => { if ((e as Error).name !== "AbortError") { setError(e); setData(null); } })
      .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    return () => ctrl.abort();
  }, [q, mode, supplier, dateFrom, dateTo, status, page]);

  const go = (next: Partial<typeof form> & { page?: number }) => {
    const m = { ...form, ...next };
    router.push(`/search${query({ q: m.q.trim(), mode: m.mode, supplier: m.supplier.trim(), date_from: m.date_from, date_to: m.date_to, status: m.status, page: next.page && next.page > 1 ? next.page : undefined })}`);
  };
  const filtersActive = !!(supplier || dateFrom || dateTo || status);
  const clearFilters = () => go({ supplier: "", date_from: "", date_to: "", status: "", page: 1 });
  const semanticDown = error instanceof ApiError && error.code === "search_unavailable";

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Search</h1>

      <form className="card space-y-3" onSubmit={(e) => { e.preventDefault(); go({ page: 1 }); }} role="search">
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="flex-1">
            <label className="label" htmlFor="q">Search for</label>
            <input id="q" className="input" value={form.q} onChange={(e) => setForm({ ...form, q: e.target.value })} placeholder='supplier, invoice number, product… or "exact phrase"' autoFocus />
          </div>
          <button className="btn btn-primary self-end" type="submit" disabled={!form.q.trim()}>Search</button>
        </div>

        <fieldset>
          <legend className="label">Method</legend>
          <div className="inline-flex flex-wrap overflow-hidden rounded-md border border-slate-300" role="radiogroup" aria-label="Search method">
            {MODES.map((m) => (
              <button key={m.id} type="button" role="radio" aria-checked={form.mode === m.id} title={m.hint}
                onClick={() => { setForm({ ...form, mode: m.id }); if (q.trim()) go({ mode: m.id, page: 1 }); }}
                className={cx("px-3 py-1.5 text-sm font-medium", form.mode === m.id ? "bg-slate-900 text-white" : "bg-white text-slate-700 hover:bg-slate-50")}>
                {m.label}
              </button>
            ))}
          </div>
          <p className="mt-1 text-xs text-slate-500">{MODES.find((m) => m.id === form.mode)?.hint}</p>
        </fieldset>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div><label className="label" htmlFor="supplier">Supplier contains</label>
            <input id="supplier" className="input" value={form.supplier} onChange={(e) => setForm({ ...form, supplier: e.target.value })} /></div>
          <div><label className="label" htmlFor="from">Invoice date from</label>
            <input id="from" type="date" className="input" value={form.date_from} onChange={(e) => setForm({ ...form, date_from: e.target.value })} /></div>
          <div><label className="label" htmlFor="to">Invoice date to</label>
            <input id="to" type="date" className="input" value={form.date_to} onChange={(e) => setForm({ ...form, date_to: e.target.value })} /></div>
          <div><label className="label" htmlFor="status">Status</label>
            <select id="status" className="input" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
              <option value="">Any</option>{DOC_STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}</select></div>
        </div>
        {filtersActive && <button type="button" className="btn" onClick={clearFilters}>Clear filters</button>}
      </form>

      {!q.trim() && <EmptyState title="Type something to search" hint="Search finds words inside the documents and filters by supplier, date and status." />}
      {loading && <LoadingBlock label="Searching…" />}
      {error != null && !loading && (
        <div className="space-y-2">
          <ErrorState error={error} title={semanticDown ? "Semantic search is not available" : "Search failed"} />
          {semanticDown && <button className="btn" onClick={() => go({ mode: "keyword", page: 1 })}>Use keyword search instead</button>}
        </div>
      )}

      {data && !loading && (
        data.items.length === 0 ? (
          <EmptyState title={`No results for “${data.query}”`} hint={filtersActive ? "Try removing a filter." : "Try different words, or another method."}
            action={filtersActive ? { onClick: clearFilters, label: "Clear filters" } : undefined} />
        ) : (
          <>
            <p className="text-sm text-slate-600">{data.total}{data.truncated ? "+" : ""} result{data.total === 1 ? "" : "s"}{data.truncated ? " (showing the best matches)" : ""}</p>
            <ul className="space-y-3">
              {data.items.map((h) => (
                <li key={h.document_id} className="card">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <Link href={`/documents/${h.document_id}`} className="min-w-0 break-all font-medium text-blue-700 hover:underline">{h.filename}</Link>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <StatusBadge status={h.status} />
                      {h.matched_by.map((m) => <Pill key={m} tone={m === "keyword" ? "blue" : "green"}>{m}</Pill>)}
                      <span className="text-xs tabular-nums text-slate-400" title="Relevance score">score {h.score.toFixed(3)}</span>
                    </div>
                  </div>
                  {h.invoice && (
                    <p className="mt-1 text-sm text-slate-600">
                      {[h.invoice.invoice_number, h.invoice.supplier_name, h.invoice.invoice_date, h.invoice.total ? `${h.invoice.total} ${h.invoice.currency ?? ""}`.trim() : null]
                        .filter(Boolean).join(" · ")}
                      {h.invoice_count > 1 && <span className="text-slate-400"> (+{h.invoice_count - 1} more invoice{h.invoice_count > 2 ? "s" : ""})</span>}
                    </p>
                  )}
                  {h.snippet && (
                    <p className="mt-2 rounded bg-slate-50 px-3 py-2 text-sm leading-relaxed text-slate-700">
                      {h.snippet.page != null && <span className="mr-1 text-xs font-medium text-slate-400">p.{h.snippet.page}</span>}
                      <Highlighted text={h.snippet.text} ranges={h.snippet.highlights} />
                    </p>
                  )}
                </li>
              ))}
            </ul>
            <Pagination page={data.page} pages={data.pages} onPage={(p) => go({ page: p })} />
          </>
        )
      )}
    </div>
  );
}

export default function SearchPage() {
  return <Suspense fallback={<LoadingBlock />}><SearchView /></Suspense>;
}
