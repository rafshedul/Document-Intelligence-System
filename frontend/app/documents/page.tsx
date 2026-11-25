"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { EmptyState, ErrorState, LoadingBlock, Pagination, Pill, ProgressBar, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { formatDateTime, statusLabel } from "@/lib/format";
import { POLL_MS, useInterval } from "@/lib/hooks";
import { DOC_STATUSES, isActive, type DocumentList, type DocumentListItem } from "@/lib/types";

const FILE_TYPES = ["pdf", "jpeg", "png", "tiff", "xlsx", "xls", "csv"];
const PAGE_SIZE = 15;

function Counts({ d }: { d: DocumentListItem }) {
  return (
    <span className="flex flex-wrap gap-1">
      {d.flagged && <Pill tone="amber">flagged</Pill>}
      {d.errors > 0 && <Pill tone="red">{d.errors} error{d.errors > 1 ? "s" : ""}</Pill>}
      {d.warnings > 0 && <Pill tone="amber">{d.warnings} warning{d.warnings > 1 ? "s" : ""}</Pill>}
    </span>
  );
}

function Live({ d }: { d: DocumentListItem }) {
  if (!isActive(d.status)) return null;
  return (
    <div className="mt-1.5 max-w-xs">
      <ProgressBar value={d.progress} label={`Progress of ${d.filename}`} />
      {d.stage && d.stage !== "queued" && <p className="mt-0.5 text-xs text-slate-500">{d.stage.replace(/_/g, " ")}</p>}
    </div>
  );
}

export default function DocumentsPage() {
  const [status, setStatus] = useState("");
  const [fileType, setFileType] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<DocumentList | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const load = useCallback(async (silent = false) => {
    const mine = ++seq.current; // ignore answers that arrive after a newer request
    if (!silent) setLoading(true);
    try {
      const r = await api.listDocuments({ status: status || undefined, file_type: fileType || undefined, page, page_size: PAGE_SIZE });
      if (mine !== seq.current) return;
      if (r.pages > 0 && page > r.pages) { setPage(r.pages); return; } // the last page just emptied
      setData(r); setError(null);
    } catch (e) {
      if (mine === seq.current && !silent) setError(e);
    } finally {
      if (mine === seq.current && !silent) setLoading(false);
    }
  }, [status, fileType, page]);

  useEffect(() => { void load(); }, [load]);
  const anyActive = !!data?.items.some((d) => isActive(d.status));
  useInterval(() => void load(true), anyActive ? POLL_MS : null);

  const filtered = !!(status || fileType);
  const clear = () => { setStatus(""); setFileType(""); setPage(1); };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Documents{data ? <span className="ml-2 text-base font-normal text-slate-500">({data.total})</span> : null}</h1>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="label" htmlFor="f-status">Status</label>
            <select id="f-status" className="input w-40" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
              <option value="">All</option>
              {DOC_STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="f-type">Type</label>
            <select id="f-type" className="input w-32" value={fileType} onChange={(e) => { setFileType(e.target.value); setPage(1); }}>
              <option value="">All</option>
              {FILE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          {filtered && <button className="btn" onClick={clear}>Clear filters</button>}
          <Link href="/upload" className="btn btn-primary">Upload</Link>
        </div>
      </div>

      {loading && !data && <LoadingBlock label="Loading documents…" />}
      {error != null && !data && <ErrorState error={error} onRetry={() => void load()} title="Could not load the documents" />}
      {error != null && data && <ErrorState error={error} onRetry={() => void load()} title="Could not refresh" />}

      {data && data.items.length === 0 && (filtered
        ? <EmptyState title="No documents match these filters" action={{ onClick: clear, label: "Clear filters" }} />
        : <EmptyState title="No documents yet" hint="Upload invoices to get started." action={{ href: "/upload", label: "Upload documents" }} />)}

      {data && data.items.length > 0 && (
        <>
          {/* wide screens: a table */}
          <div className="card hidden overflow-x-auto p-0 md:block">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr><th className="px-4 py-2.5">Name</th><th className="px-4 py-2.5">Type</th><th className="px-4 py-2.5">Status</th><th className="px-4 py-2.5">Uploaded</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.items.map((d) => (
                  <tr key={d.id} className="align-top hover:bg-slate-50">
                    <td className="px-4 py-3">
                      <Link href={`/documents/${d.id}`} className="font-medium text-blue-700 hover:underline">{d.filename}</Link>
                      <Live d={d} />
                    </td>
                    <td className="px-4 py-3 uppercase text-slate-600">{d.file_type}{d.page_count ? <span className="ml-1 normal-case text-slate-400">· {d.page_count} p</span> : null}</td>
                    <td className="px-4 py-3"><div className="flex flex-col items-start gap-1"><StatusBadge status={d.status} /><Counts d={d} /></div></td>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">{formatDateTime(d.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* phones: cards */}
          <ul className="space-y-2 md:hidden">
            {data.items.map((d) => (
              <li key={d.id} className="card">
                <div className="flex items-start justify-between gap-2">
                  <Link href={`/documents/${d.id}`} className="min-w-0 break-words font-medium text-blue-700">{d.filename}</Link>
                  <StatusBadge status={d.status} />
                </div>
                <p className="mt-1 text-xs text-slate-500"><span className="uppercase">{d.file_type}</span> · {formatDateTime(d.created_at)}</p>
                <div className="mt-1"><Counts d={d} /></div>
                <Live d={d} />
              </li>
            ))}
          </ul>
          <Pagination page={data.page} pages={data.pages} onPage={setPage} />
        </>
      )}
    </div>
  );
}
