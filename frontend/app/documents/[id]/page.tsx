"use client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CorrectionHistory } from "@/components/CorrectionHistory";
import { FieldsPanel } from "@/components/FieldsPanel";
import { DownloadButton, FilePreview } from "@/components/FilePreview";
import { EmptyState, ErrorState, LoadingBlock, Pill, ProgressBar, StatusBadge, cx } from "@/components/ui";
import { ValidationPanel } from "@/components/ValidationPanel";
import { ApiError, api } from "@/lib/api";
import { buildCorrection, emptyDraft, itemKey, markRows, validateDraft, type Draft } from "@/lib/edits";
import { formatBytes, formatDateTime } from "@/lib/format";
import { POLL_MS, useInterval } from "@/lib/hooks";
import { isActive, type CorrectionEntry, type FieldName, type ItemField, type ResultResponse, type StatusResponse } from "@/lib/types";

export default function DocumentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [result, setResult] = useState<ResultResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState<StatusResponse | null>(null);
  const [corrections, setCorrections] = useState<CorrectionEntry[] | null>(null);
  const [corrError, setCorrError] = useState<unknown>(null);
  const [corrLoading, setCorrLoading] = useState(false);
  const [invoiceIdx, setInvoiceIdx] = useState(0);
  const [previewOpen, setPreviewOpen] = useState(true);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [saving, setSaving] = useState(false);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [focusField, setFocusField] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [busy, setBusy] = useState<"reprocess" | "delete" | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout>>();

  const say = useCallback((tone: "ok" | "error", text: string) => {
    setNotice({ tone, text });
    clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), 6000);
  }, []);
  useEffect(() => () => clearTimeout(noticeTimer.current), []);

  const loadResult = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try { setResult(await api.result(id)); setError(null); }
    catch (e) { if (!silent) setError(e); }
    finally { if (!silent) setLoading(false); }
  }, [id]);

  const loadCorrections = useCallback(async () => {
    setCorrLoading(true);
    try { setCorrections(await api.corrections(id)); setCorrError(null); }
    catch (e) { setCorrError(e); }
    finally { setCorrLoading(false); }
  }, [id]);

  useEffect(() => { void loadResult(); void loadCorrections(); }, [loadResult, loadCorrections]);

  // While the document is being processed, ask for its progress every 2 s; reload everything when it finishes.
  const status = result?.document.status;
  const active = status ? isActive(status) : false;
  useInterval(async () => {
    try {
      const s = await api.status(id);
      setLive(s);
      if (!isActive(s.status)) { setLive(null); await loadResult(true); await loadCorrections(); }
    } catch { /* a missed poll is fine; the next one will try again */ }
  }, active ? POLL_MS : null);

  const doc = result?.document;
  const invoices = result?.results ?? [];
  const invoice = invoices[invoiceIdx] ?? invoices[0];
  const marks = useMemo(() => markRows(result?.findings ?? [], invoice?.id), [result, invoice?.id]);
  const canEdit = !!invoice && !!doc && !isActive(doc.status);

  function startEdit(focus?: string) { setEditing(true); setFocusField(focus ?? null); setSaveError(null); }
  function cancelEdit() { setEditing(false); setDraft(emptyDraft()); setProblems({}); setSaveError(null); setFocusField(null); }
  const setField = (name: FieldName, value: string) => { setDraft((d) => ({ ...d, fields: { ...d.fields, [name]: value } })); setProblems((p) => { const { [name]: _, ...rest } = p; return rest; }); };
  const setItem = (pos: number, f: ItemField, value: string) => { const k = itemKey(pos, f); setDraft((d) => ({ ...d, items: { ...d.items, [k]: value } })); setProblems((p) => { const { [k]: _, ...rest } = p; return rest; }); };

  async function save() {
    if (!invoice) return;
    const local = validateDraft(draft);
    if (Object.keys(local).length) { setProblems(local); setSaveError("Some values need fixing before they can be saved."); return; }
    const body = buildCorrection(invoice, draft);
    if (!body) { cancelEdit(); say("ok", "Nothing was changed."); return; }
    setSaving(true); setSaveError(null);
    try {
      const updated = await api.correct(id, body);
      setResult(updated);
      cancelEdit();
      void loadCorrections();
      say("ok", "Saved. Validation ran again with your changes.");
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) { setProblems(e.problems); setSaveError("The server did not accept some values."); }
      else setSaveError(e instanceof Error ? e.message : "Saving failed.");
    } finally { setSaving(false); }
  }

  async function reprocess() {
    if (!doc) return;
    if (invoices.some((i) => i.is_manually_corrected) &&
        !confirm("This document has manual corrections. Reprocessing creates a new extraction without them (the old version and the correction log are kept). Continue?")) return;
    setBusy("reprocess");
    try {
      await api.reprocess(id);
      setResult((r) => r && { ...r, document: { ...r.document, status: "queued", stage: "queued", progress: 0, error_message: null } });
      cancelEdit();
      say("ok", "Queued again. The progress will update here.");
    } catch (e) { say("error", e instanceof Error ? e.message : "Could not reprocess."); }
    finally { setBusy(null); }
  }

  async function remove() {
    if (!doc || !confirm(`Delete “${doc.filename}” and everything extracted from it? This cannot be undone.`)) return;
    setBusy("delete");
    try { await api.remove(id); router.push("/documents"); }
    catch (e) { say("error", e instanceof Error ? e.message : "Could not delete."); setBusy(null); }
  }

  function jump(rowId: string) {
    document.getElementById(rowId)?.scrollIntoView({ behavior: "smooth", block: "center" });
    setFlash(rowId);
    setTimeout(() => setFlash(null), 1600);
  }

  if (loading) return <LoadingBlock label="Loading document…" />;
  if (error != null || !result || !doc) {
    const gone = error instanceof ApiError && error.status === 404;
    return (
      <div className="mx-auto max-w-xl space-y-3">
        <ErrorState error={error ?? new Error("Not found")} title={gone ? "This document does not exist (it may have been deleted)" : "Could not load the document"}
          onRetry={gone ? undefined : () => void loadResult()} />
        <Link href="/documents" className="btn">← Back to documents</Link>
      </div>
    );
  }

  const progress = live?.progress ?? doc.progress;
  const stage = live?.stage ?? doc.stage;
  const message = live?.error_message ?? doc.error_message;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/documents" className="text-sm text-slate-500 hover:text-slate-800">← Documents</Link>
          <h1 className="mt-1 flex flex-wrap items-center gap-2 break-words text-xl font-semibold">
            <span className="min-w-0 break-all">{doc.filename}</span>
            <StatusBadge status={live?.status ?? doc.status} />
            {doc.flagged && <Pill tone="amber">flagged: has warnings</Pill>}
          </h1>
          <p className="mt-0.5 text-xs text-slate-500">
            <span className="uppercase">{doc.file_type}</span> · {formatBytes(doc.file_size)}{doc.page_count ? ` · ${doc.page_count} page${doc.page_count > 1 ? "s" : ""}` : ""} · uploaded {formatDateTime(doc.created_at)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <DownloadButton id={id} filename={doc.filename} />
          {(doc.status === "failed" || doc.status === "needs_review") && (
            <button className="btn btn-primary" onClick={reprocess} disabled={busy !== null}>{busy === "reprocess" ? "Queueing…" : "Reprocess"}</button>
          )}
          {canEdit && !editing && <button className="btn" onClick={() => startEdit()}>Edit values</button>}
          <button className="btn btn-danger" onClick={remove} disabled={busy !== null}>{busy === "delete" ? "Deleting…" : "Delete"}</button>
        </div>
      </div>

      {notice && (
        <div role="status" className={cx("rounded-md px-3 py-2 text-sm", notice.tone === "ok" ? "bg-green-50 text-green-800" : "bg-red-50 text-red-800")}>{notice.text}</div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* left: the original file */}
        <div className="min-w-0">
          <button className="btn mb-2 w-full lg:hidden" onClick={() => setPreviewOpen((o) => !o)}>{previewOpen ? "Hide original file" : "Show original file"}</button>
          <div className={cx("overflow-hidden rounded-lg border border-slate-200 bg-white lg:sticky lg:top-16 lg:block lg:h-[calc(100vh-5.5rem)]",
            previewOpen ? "block h-[28rem]" : "hidden")}>
            <FilePreview id={id} fileType={doc.file_type} filename={doc.filename} />
          </div>
        </div>

        {/* right: data */}
        <div className="min-w-0 space-y-4">
          {isActive(live?.status ?? doc.status) && (
            <section className="card" aria-live="polite">
              <p className="mb-2 text-sm font-medium">Processing{stage && stage !== "queued" ? `: ${stage.replace(/_/g, " ")}` : "… waiting in the queue"}</p>
              <ProgressBar value={progress} label="Processing progress" />
              <p className="mt-2 text-xs text-slate-500">This page updates by itself every {POLL_MS / 1000} seconds.</p>
            </section>
          )}
          {!isActive(doc.status) && message && (
            <div className={cx("rounded-md px-3 py-2 text-sm", doc.status === "failed" ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-900")} role="alert">
              <p className="font-medium">{doc.status === "failed" ? "Processing failed" : "Needs review"}</p>
              <p>{message}</p>
              {doc.status === "failed" && <p className="mt-1 text-xs opacity-80">You can press Reprocess to try again.</p>}
            </div>
          )}

          {invoices.length > 1 && (
            <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Invoices in this document">
              {invoices.map((inv, i) => (
                <button key={inv.id} role="tab" aria-selected={i === invoiceIdx} disabled={editing}
                  className={cx("rounded-md border px-2.5 py-1 text-sm", i === invoiceIdx ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50")}
                  onClick={() => setInvoiceIdx(i)}>
                  {inv.fields.invoice_number.value ?? `Invoice ${i + 1}`}
                </button>
              ))}
            </div>
          )}

          {editing && (
            <div className="sticky top-14 z-10 flex flex-wrap items-center gap-2 rounded-md border border-blue-300 bg-blue-50 px-3 py-2 text-sm shadow-sm">
              <span className="font-medium text-blue-900">Editing</span>
              <span className="text-blue-800">Change what is wrong, then save. Validation will run again.</span>
              <span className="ml-auto flex gap-2">
                <button className="btn" onClick={cancelEdit} disabled={saving}>Cancel</button>
                <button className="btn btn-primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
              </span>
              {saveError && <p className="basis-full text-red-700" role="alert">{saveError}</p>}
            </div>
          )}

          {invoice ? (
            <FieldsPanel invoice={invoice} marks={marks} editing={editing} canEdit={canEdit} draft={draft} problems={problems} flash={flash}
              focusField={focusField} onEdit={startEdit} onField={setField} onItem={setItem} />
          ) : !isActive(doc.status) ? (
            <EmptyState title="No data was extracted from this document" hint={doc.status === "failed" ? "Processing failed, see the message above." : "There was nothing to read, or extraction found no invoice."} />
          ) : null}

          <ValidationPanel findings={result.findings} invoices={invoices} status={doc.status} onJump={jump} />
          <CorrectionHistory entries={corrections} loading={corrLoading} error={corrError} multi={invoices.length > 1} onRetry={() => void loadCorrections()} />
        </div>
      </div>
    </div>
  );
}
