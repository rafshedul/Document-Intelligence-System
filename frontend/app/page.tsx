"use client";
import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Row = { id: string; filename: string; status: string; flagged: boolean; file_type: string; page_count: number | null;
  created_at: string; errors: number; warnings: number };
type Field = { value: string | null; confidence?: number; source_snippet?: string | null };
type Detail = {
  id: string; filename: string; status: string; flagged: boolean; file_type: string; file_size: number;
  page_count: number | null; error_message: string | null;
  history: { stage: string; status: string; duration_ms: number | null; details: Record<string, any> | null }[];
  pages: { page: number; method: string; ocr_confidence: string | null; text: string }[];
  results: { id: string; invoice_index: number; pages: number[]; source: string | null; fields: Record<string, Field>;
    line_items: { position: number; description: string | null; quantity: string | null; unit_price: string | null; line_total: string | null }[] }[];
  findings: { rule_code: string; severity: string; field: string | null; message: string; resolved: boolean }[];
};

const FIELD_LABELS: Record<string, string> = { invoice_number: "Invoice number", invoice_date: "Invoice date", supplier_name: "Supplier",
  customer_name: "Customer", subtotal: "Subtotal", tax: "Tax", total: "Total", currency: "Currency" };
const MONEY = new Set(["subtotal", "tax", "total"]);
const ACTIVE = new Set(["queued", "processing"]);

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

  const loadList = useCallback(async () => {
    try { const r = await fetch(`${API}/api/documents`); setRows(await r.json()); setOffline(false); } catch { setOffline(true); }
  }, []);
  const loadDetail = useCallback(async (id: string) => {
    try { const r = await fetch(`${API}/api/documents/${id}`); if (r.ok) setDetail(await r.json()); } catch {}
  }, []);

  useEffect(() => { fetch(`${API}/api/info`).then(r => r.json()).then(setInfo).catch(() => {}); loadList(); }, [loadList]);
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
    for (const f of Array.from(files)) {
      const fd = new FormData(); fd.append("file", f);
      try {
        const r = await fetch(`${API}/api/documents`, { method: "POST", body: fd });
        const j = await r.json();
        if (r.status === 202) setSel(j.id);
        else if (r.status === 409) { setMsg(`"${f.name}": ${j.detail.message} (as "${j.detail.filename}")`); setSel(j.detail.document_id); }
        else setMsg(`"${f.name}": ${typeof j.detail === "string" ? j.detail : "upload failed"}`);
      } catch { setMsg("Cannot reach the backend at " + API); }
    }
    loadList();
  }

  const demo = info?.llm_provider === "demo";
  return (<>
    <header>
      <h1>Document Intelligence System</h1>
      {offline && <span className="badge failed">backend offline</span>}
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
          {msg && <div className="msg">{msg}</div>}
        </div>
        <div className="card">
          <h2>Documents ({rows.length})</h2>
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
            </div>))}
        </div>
      </div>

      <div className="grid">
        {!detail && <div className="card empty">Select or upload a document to see what the system did with it.</div>}
        {detail && <>
          <div className="card">
            <div className="row"><h2 style={{ margin: 0 }}>{detail.filename}</h2><Badge s={detail.status} />
              {detail.flagged && <Badge s="flag">flagged: completed with warnings</Badge>}</div>
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
                  <td className={MONEY.has(k) ? "num" : ""}>{f.value ?? <span className="null">null</span>}</td>
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
