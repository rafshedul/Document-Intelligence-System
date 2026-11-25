"use client";
import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const TOKEN_KEY = "docintel_token";

type Row = { id: string; filename: string; status: string; stage: string | null; progress: number; flagged: boolean; file_type: string;
  page_count: number | null; created_at: string; errors: number; warnings: number };
type Field = { value: string | null; confidence?: number; source_snippet?: string | null; manual?: boolean };
type Detail = {
  id: string; filename: string; status: string; stage: string | null; progress: number; flagged: boolean; file_type: string;
  file_size: number; page_count: number | null; error_message: string | null;
  history: { stage: string; status: string; duration_ms: number | null; details: Record<string, any> | null }[];
  pages: { page: number; method: string; ocr_confidence: string | null; text: string }[];
  results: { id: string; invoice_index: number; pages: (number | null)[]; source: string | null; is_manually_corrected?: boolean; fields: Record<string, Field>;
    line_items: { position: number; description: string | null; quantity: string | null; unit_price: string | null; line_total: string | null }[] }[];
  findings: { rule_code: string; severity: string; field: string | null; message: string; resolved: boolean }[];
};

type Hit = { document_id: string; filename: string; status: string; file_type: string; score: number; matched_by: string[];
  snippet: { text: string; highlights: number[][]; page: number | null } | null; invoice_count: number;
  invoice: { invoice_number: string | null; invoice_date: string | null; supplier_name: string | null; total: string | null; currency: string | null } | null };
type SearchResult = { items: Hit[]; total: number; page: number; pages: number; truncated: boolean };

// Highlights come from the server as offsets into plain text; React escapes the text, so nothing is parsed as HTML.
const Snippet = ({ s }: { s: NonNullable<Hit["snippet"]> }) => {
  const out: React.ReactNode[] = []; let at = 0;
  s.highlights.forEach(([a, b], i) => { out.push(s.text.slice(at, a), <mark key={i}>{s.text.slice(a, b)}</mark>); at = b; });
  out.push(s.text.slice(at));
  return <span>{out}</span>;
};

const FIELD_LABELS: Record<string, string> = { invoice_number: "Invoice number", invoice_date: "Invoice date", supplier_name: "Supplier",
  customer_name: "Customer", subtotal: "Subtotal", tax: "Tax", total: "Total", currency: "Currency" };
const MONEY = new Set(["subtotal", "tax", "total"]);
const ACTIVE = new Set(["queued", "processing"]);
const PAGE_SIZE = 10;
const errText = (j: any, fallback: string) => j?.error?.message ? j.error.message + (j.error.details?.problems ? ": " + j.error.details.problems.map((p: any) => `${p.field} ${p.message}`).join("; ") : "") : fallback;

const Progress = ({ p, stage }: { p: number; stage?: string | null }) => (
  <span title={stage ?? ""}><span className="bar" style={{ width: 90 }}><i style={{ width: `${p}%` }} /></span>{p}%{stage && stage !== "done" ? ` · ${stage}` : ""}</span>);

const Badge = ({ s, children }: { s: string; children?: React.ReactNode }) => <span className={`badge ${s}`}>{children ?? s.replace("_", " ")}</span>;
const Conf = ({ c }: { c?: number }) => c === undefined ? null :
  <span><span className={`bar ${c < 0.7 ? "low" : ""}`}><i style={{ width: `${c * 100}%` }} /></span>{c.toFixed(2)}</span>;

export default function Home() {
  const [rows, setRows] = useState<Row[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [info, setInfo] = useState<{ llm_provider: string; llm_model: string; extensions: string[] } | null>(null);
  const [msg, setMsg] = useState("");
  const [over, setOver] = useState(false);
  const [offline, setOffline] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [sq, setSq] = useState("");
  const [smode, setSmode] = useState("hybrid");
  const [ssupplier, setSsupplier] = useState("");
  const [sfrom, setSfrom] = useState("");
  const [sto, setSto] = useState("");
  const [sres, setSres] = useState<SearchResult | null>(null);
  const [sbusy, setSbusy] = useState(false);
  const [needLogin, setNeedLogin] = useState(false);
  const [signedIn, setSignedIn] = useState(false);  // set after mount: sessionStorage is not available during SSR
  useEffect(() => setSignedIn(!!sessionStorage.getItem(TOKEN_KEY)), [needLogin]);
  const [tokenInput, setTokenInput] = useState("");
  const [loginError, setLoginError] = useState("");

  // fetch with the saved token; a 401 sends the user to the login box
  const api = useCallback(async (path: string, init: RequestInit = {}) => {
    const t = sessionStorage.getItem(TOKEN_KEY);
    const headers = { ...(init.headers as Record<string, string>), ...(t ? { Authorization: `Bearer ${t}` } : {}) };
    const r = await fetch(`${API}${path}`, { ...init, headers });
    if (r.status === 401) { sessionStorage.removeItem(TOKEN_KEY); setNeedLogin(true); }
    return r;
  }, []);

  const loadList = useCallback(async () => {
    try { const q = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) }); if (statusFilter) q.set("status", statusFilter);
      const r = await api(`/api/documents?${q}`);
      if (r.ok) { const j = await r.json(); setRows(j.items); setTotal(j.total); setPages(Math.max(1, j.pages)); setNeedLogin(false); }
      setOffline(false); } catch { setOffline(true); }
  }, [api, page, statusFilter]);
  const loadDetail = useCallback(async (id: string) => {
    try { const r = await api(`/api/documents/${id}`);
      if (r.ok) { const j = await r.json(); setDetail({ ...j.document, results: j.results, findings: j.findings, history: j.history, pages: j.pages }); }
      else if (r.status === 404) { setSel(null); setDetail(null); } } catch {}
  }, [api]);

  useEffect(() => { api("/api/info").then(r => r.ok ? r.json() : null).then(j => j && setInfo(j)).catch(() => {}); loadList(); }, [api, loadList, needLogin]);
  useEffect(() => { if (sel) loadDetail(sel); else setDetail(null); }, [sel, loadDetail]);
  useEffect(() => {  // poll while anything is still running
    const busy = rows.some(r => ACTIVE.has(r.status)) || (detail && ACTIVE.has(detail.status));
    if (!busy) return;
    const t = setInterval(() => { loadList(); if (sel) loadDetail(sel); }, 1500);
    return () => clearInterval(t);
  }, [rows, detail, sel, loadList, loadDetail]);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setMsg("");
    const fd = new FormData();
    Array.from(files).forEach(f => fd.append("files", f));  // all files in one request
    try {
      const r = await api("/api/documents/upload", { method: "POST", body: fd });
      const j = await r.json().catch(() => ({}));
      const results = r.status === 202 ? j.files : j?.error?.details?.files ?? [];
      const bad = results.filter((f: any) => f.status === "rejected");
      const lines = bad.map((f: any) => `"${f.filename}": ${f.error.message}`);
      if (r.status !== 202 && lines.length === 0) lines.push(errText(j, "Upload failed."));
      if (r.status === 429) lines.push("Wait a moment, then try again.");
      setMsg(lines.join("\n"));
      const firstOk = results.find((f: any) => f.status === "queued") ?? bad.find((f: any) => f.error?.existing_document_id);
      if (firstOk) setSel(firstOk.id ?? firstOk.error.existing_document_id);
    } catch { setMsg("Cannot reach the backend at " + API); }
    setPage(1);
    loadList();
  }

  async function runSearch(pageNo = 1) {
    if (!sq.trim()) { setSres(null); return; }
    setSbusy(true); setMsg("");
    const q = new URLSearchParams({ q: sq, mode: smode, page: String(pageNo), page_size: "5" });
    if (ssupplier.trim()) q.set("supplier", ssupplier.trim());
    if (sfrom) q.set("date_from", sfrom);
    if (sto) q.set("date_to", sto);
    try {
      const r = await api(`/api/search?${q}`);
      const j = await r.json().catch(() => ({}));
      if (r.ok) setSres(j); else { setSres(null); setMsg(errText(j, "Search failed.")); }
    } catch { setMsg("Cannot reach the backend at " + API); }
    setSbusy(false);
  }

  async function reprocess() {
    if (!detail) return;
    if (detail.results.some(x => x.is_manually_corrected) &&
        !confirm("This document has manual corrections. Reprocessing creates a new extraction without them (the old version and the correction log are kept). Continue?")) return;
    const r = await api(`/api/documents/${detail.id}/reprocess`, { method: "POST" });
    if (!r.ok) setMsg(errText(await r.json().catch(() => ({})), "Could not reprocess."));
    loadList(); loadDetail(detail.id);
  }

  async function remove() {
    if (!detail || !confirm(`Delete "${detail.filename}" and all its data?`)) return;
    const r = await api(`/api/documents/${detail.id}`, { method: "DELETE" });
    if (r.ok) { setSel(null); setDetail(null); loadList(); }
    else setMsg(errText(await r.json().catch(() => ({})), "Could not delete."));
  }

  async function correct(invoiceIndex: number, field: string, current: string | null) {
    if (!detail) return;
    const v = window.prompt(`New value for "${FIELD_LABELS[field] ?? field}" (empty clears it):`, current ?? "");
    if (v === null) return;
    const r = await api(`/api/documents/${detail.id}/result`, { method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invoice_index: invoiceIndex, fields: { [field]: v }, corrected_by: "web" }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { setMsg(errText(j, "Could not save the correction.")); return; }
    setMsg(""); loadList(); loadDetail(detail.id);
  }

  async function login(e: React.FormEvent) {
    e.preventDefault();
    const r = await fetch(`${API}/api/info`, { headers: { Authorization: `Bearer ${tokenInput.trim()}` } });
    if (r.ok) { sessionStorage.setItem(TOKEN_KEY, tokenInput.trim()); setTokenInput(""); setLoginError(""); setNeedLogin(false); setSignedIn(true); loadList(); }
    else setLoginError(r.status === 401 ? "Wrong token." : `Server error (${r.status}).`);
  }

  const demo = info?.llm_provider === "demo";
  if (needLogin) return (
    <main style={{ display: "block", maxWidth: 420, margin: "80px auto" }}>
      <form className="card" onSubmit={login}>
        <h2>Sign in</h2>
        <p style={{ marginTop: 0 }}>Enter the API token (<code>API_AUTH_TOKEN</code> from the server's <code>.env</code>).</p>
        <input type="password" autoFocus value={tokenInput} onChange={e => setTokenInput(e.target.value)}
          placeholder="token" style={{ width: "100%", padding: 8, border: "1px solid var(--line)", borderRadius: 6 }} />
        {loginError && <div className="msg">{loginError}</div>}
        <button type="submit" style={{ marginTop: 10, padding: "8px 16px" }}>Sign in</button>
      </form>
    </main>);
  return (<>
    <header>
      <h1>Document Intelligence System</h1>
      {offline && <span className="badge failed">backend offline</span>}
      {signedIn && <button style={{ marginLeft: "auto" }} onClick={() => { sessionStorage.removeItem(TOKEN_KEY); setNeedLogin(true); }}>Sign out</button>}
      {info && <span className={`banner ${demo ? "demo" : ""}`}>
        {demo ? "DEMO extractor (rule-based, not an AI model)" : `LLM: ${info.llm_model}`}</span>}
    </header>
    <main>
      <div className="grid">
        <div className="card">
          <h2>Upload</h2>
          <div className={`drop ${over ? "over" : ""}`} onClick={() => input.current?.click()}
            onDragOver={e => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
            onDrop={e => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}>
            <b>Drop invoices here</b> or click to choose<br />
            <small>{info ? info.extensions.join("  ") : "PDF, JPG, PNG, TIFF, XLSX, XLS, CSV"}</small>
          </div>
          <input ref={input} type="file" multiple hidden onChange={e => { upload(e.target.files); e.target.value = ""; }} />
          {msg && <div className="msg" style={{ whiteSpace: "pre-line" }}>{msg}</div>}
        </div>
        <div className="card">
          <h2>Search</h2>
          <form onSubmit={e => { e.preventDefault(); runSearch(1); }} className="grid" style={{ gap: 8 }}>
            <div className="row">
              <input value={sq} onChange={e => setSq(e.target.value)} placeholder='words, "exact phrase", -exclude' style={{ flex: 1, padding: 6 }} />
              <select value={smode} onChange={e => setSmode(e.target.value)}>
                <option value="hybrid">hybrid</option><option value="keyword">keyword</option><option value="semantic">semantic</option></select>
              <button type="submit" disabled={sbusy}>{sbusy ? "…" : "Search"}</button>
            </div>
            <div className="row">
              <input value={ssupplier} onChange={e => setSsupplier(e.target.value)} placeholder="supplier contains" style={{ flex: 1, padding: 4 }} />
              <input type="date" value={sfrom} onChange={e => setSfrom(e.target.value)} title="invoice date from" />
              <input type="date" value={sto} onChange={e => setSto(e.target.value)} title="invoice date to" />
            </div>
          </form>
          {sres && <div style={{ marginTop: 10 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span>{sres.total} result{sres.total === 1 ? "" : "s"}{sres.truncated ? " (capped)" : ""}</span>
              <a href="#" onClick={e => { e.preventDefault(); setSres(null); setSq(""); }}>clear</a></div>
            {sres.items.map(h => (
              <div key={h.document_id} className="doc" onClick={() => setSel(h.document_id)}>
                <div className="name">{h.filename}</div>
                <div className="meta">
                  {h.matched_by.map(m => <Badge key={m} s="started">{m}</Badge>)}
                  <span>score {h.score.toFixed(4)}</span><Badge s={h.status} />
                  {h.invoice && <span>{[h.invoice.invoice_number, h.invoice.supplier_name,
                    h.invoice.total ? `${h.invoice.total} ${h.invoice.currency ?? ""}` : null].filter(Boolean).join(" · ")}</span>}
                </div>
                {h.snippet && <div className="snip" style={{ marginTop: 4 }}>
                  {h.snippet.page ? `p.${h.snippet.page}: ` : ""}<Snippet s={h.snippet} /></div>}
              </div>))}
            {sres.pages > 1 && <div className="row" style={{ marginTop: 8 }}>
              <button disabled={sres.page <= 1} onClick={() => runSearch(sres.page - 1)}>‹ Prev</button>
              <span>page {sres.page} of {sres.pages}</span>
              <button disabled={sres.page >= sres.pages} onClick={() => runSearch(sres.page + 1)}>Next ›</button></div>}
          </div>}
        </div>
        <div className="card">
          <h2>Documents ({total})</h2>
          <select value={statusFilter} onChange={e => { setStatusFilter(e.target.value); setPage(1); }} style={{ marginBottom: 4 }}>
            <option value="">all statuses</option>
            {["queued", "processing", "completed", "needs_review", "failed"].map(x => <option key={x} value={x}>{x.replace("_", " ")}</option>)}
          </select>
          {rows.length === 0 && <div className="empty">Nothing yet. Try a file from <code>sample_data/</code>.</div>}
          {rows.map(r => (
            <div key={r.id} className={`doc ${sel === r.id ? "sel" : ""}`} onClick={() => setSel(r.id)}>
              <div className="name">{r.filename}</div>
              <div className="meta">
                <Badge s={r.status} />{r.flagged && <Badge s="flag">flagged</Badge>}
                {r.errors > 0 && <Badge s="error">{r.errors} error{r.errors > 1 ? "s" : ""}</Badge>}
                {r.warnings > 0 && <Badge s="warning">{r.warnings} warning{r.warnings > 1 ? "s" : ""}</Badge>}
                <span>{r.file_type}{r.page_count ? ` · ${r.page_count} p` : ""}</span>
              </div>
              {ACTIVE.has(r.status) && <div className="meta"><Progress p={r.progress} stage={r.stage} /></div>}
            </div>))}
          {pages > 1 && <div className="row" style={{ marginTop: 10 }}>
            <button disabled={page <= 1} onClick={() => setPage(page - 1)}>‹ Prev</button>
            <span>page {page} of {pages}</span>
            <button disabled={page >= pages} onClick={() => setPage(page + 1)}>Next ›</button></div>}
        </div>
      </div>

      <div className="grid">
        {!detail && <div className="card empty">Select or upload a document to see what the system did with it.</div>}
        {detail && <>
          <div className="card">
            <div className="row"><h2 style={{ margin: 0 }}>{detail.filename}</h2><Badge s={detail.status} />
              {detail.flagged && <Badge s="flag">flagged: completed with warnings</Badge>}
              <span style={{ marginLeft: "auto" }} className="row">
                {(detail.status === "failed" || detail.status === "needs_review") && <button onClick={reprocess}>Reprocess</button>}
                <button onClick={remove}>Delete</button></span></div>
            {ACTIVE.has(detail.status) && <div style={{ marginTop: 8 }}><Progress p={detail.progress} stage={detail.stage} /></div>}
            {detail.error_message && <p style={{ color: "var(--err)" }}>{detail.error_message}</p>}
          </div>

          <div className="card"><h2>1. Processing log</h2>
            <table><thead><tr><th>Stage</th><th>Status</th><th>Time</th><th>Details</th></tr></thead><tbody>
              {detail.history.map((h, i) => <tr key={i}><td>{h.stage}</td><td><Badge s={h.status} /></td>
                <td className="num">{h.duration_ms ?? "…"} ms</td>
                <td className="snip">{h.details ? Object.entries(h.details).filter(([k]) => !["traceback", "by_rule"].includes(k))
                  .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ").slice(0, 220) : ""}</td></tr>)}
            </tbody></table>
          </div>

          {detail.results.map(r => (
            <div className="card" key={r.id}>
              <h2>2. Extracted invoice {detail.results.length > 1 ? `#${r.invoice_index + 1} ` : ""}
                (pages {r.pages[0]}–{r.pages[1]}{r.source === "spreadsheet_mapping" ? " · mapped from columns, no LLM" : ""})</h2>
              <table><thead><tr><th>Field</th><th>Value</th><th>Confidence</th><th>Source snippet (from the document)</th></tr></thead><tbody>
                {Object.entries(r.fields).map(([k, f]) => <tr key={k}><td>{FIELD_LABELS[k] ?? k}</td>
                  <td className={MONEY.has(k) ? "num" : ""}>{f.value ?? <span className="null">null</span>}
                    {f.manual && <span title="corrected by hand" style={{ color: "var(--accent)" }}> ✎</span>}
                    {!ACTIVE.has(detail.status) && <a href="#" style={{ marginLeft: 8, fontSize: 12 }}
                      onClick={e => { e.preventDefault(); correct(r.invoice_index, k, f.value); }}>edit</a>}</td>
                  <td>{f.value !== null && <Conf c={f.confidence} />}</td><td className="snip">{f.source_snippet ?? ""}</td></tr>)}
              </tbody></table>
              {r.line_items.length > 0 && <><h2 style={{ marginTop: 16 }}>Line items</h2>
                <table><thead><tr><th>#</th><th>Description</th><th>Qty</th><th>Unit price</th><th>Line total</th></tr></thead><tbody>
                  {r.line_items.map(i => <tr key={i.position}><td>{i.position}</td><td>{i.description}</td>
                    <td className="num">{i.quantity}</td><td className="num">{i.unit_price}</td><td className="num">{i.line_total}</td></tr>)}
                </tbody></table></>}
            </div>))}

          <div className="card"><h2>3. Validation ({detail.findings.length} finding{detail.findings.length === 1 ? "" : "s"})</h2>
            {detail.findings.length === 0 && detail.status === "completed" && <Badge s="completed">all checks passed</Badge>}
            {detail.findings.map((f, i) => <div key={i} className={`find ${f.severity}`}>
              <Badge s={f.severity} /><code>{f.rule_code}</code><span>{f.message}</span></div>)}
          </div>

          <div className="card"><h2>Extracted text ({detail.pages.length} page{detail.pages.length === 1 ? "" : "s"} shown)</h2>
            {detail.pages.map(p => <details key={p.page} open={detail.pages.length === 1}>
              <summary>Page {p.page} · {p.method}{p.ocr_confidence ? ` · OCR confidence ${p.ocr_confidence}` : ""}</summary>
              <pre>{p.text || "(no text)"}</pre></details>)}
          </div>
        </>}
      </div>
    </main>
  </>);
}
