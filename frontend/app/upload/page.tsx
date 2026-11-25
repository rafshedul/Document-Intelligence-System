"use client";
import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import { useInfo } from "@/components/AuthGate";
import { ProgressBar, Spinner, cx } from "@/components/ui";
import { ApiError, uploadFile } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { checkFile, runPool } from "@/lib/upload";

type ItemState = "invalid" | "waiting" | "uploading" | "done" | "duplicate" | "error";
interface Item {
  id: number;
  file: File;
  state: ItemState;
  progress: number;
  message?: string;
  documentId?: string;
}

const CONCURRENCY = 3;
let nextId = 1;

export default function UploadPage() {
  const info = useInfo();
  const [items, setItems] = useState<Item[]>([]);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const patch = useCallback((id: number, change: Partial<Item>) =>
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...change } : it))), []);

  const send = useCallback(async (it: Item) => {
    patch(it.id, { state: "uploading", progress: 0, message: undefined });
    try {
      const r = await uploadFile(it.file, (p) => patch(it.id, { progress: p }));
      patch(it.id, { state: "done", progress: 100, documentId: r.id ?? undefined });
    } catch (e) {
      if (e instanceof ApiError && e.code === "duplicate_file") {
        patch(it.id, { state: "duplicate", message: e.message, documentId: e.existingDocumentId ?? undefined });
      } else if (e instanceof ApiError && e.code === "rate_limited") {
        patch(it.id, { state: "error", message: "Too many uploads at once. Wait a minute, then press Retry." });
      } else {
        patch(it.id, { state: "error", message: e instanceof Error ? e.message : "Upload failed." });
      }
    }
  }, [patch]);

  const add = useCallback((files: FileList | File[]) => {
    const fresh: Item[] = Array.from(files).map((file) => {
      const problem = checkFile(file, info);
      return { id: nextId++, file, state: problem ? "invalid" : "waiting", progress: 0, message: problem ?? undefined };
    });
    if (!fresh.length) return;
    setItems((prev) => [...fresh, ...prev]);
    void runPool(fresh.filter((f) => f.state === "waiting"), CONCURRENCY, send);
  }, [info, send]);

  const retryFailed = () => {
    const failed = items.filter((i) => i.state === "error");
    void runPool(failed, CONCURRENCY, send);
  };

  const done = items.filter((i) => i.state === "done").length;
  const busy = items.some((i) => i.state === "uploading" || i.state === "waiting");
  const failed = items.filter((i) => i.state === "error").length;
  const extensions = info?.extensions.join("  ") ?? ".pdf .jpg .png .tif .xlsx .xls .csv";

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <h1 className="text-xl font-semibold">Upload documents</h1>

      <div
        className={cx("flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-12 text-center transition",
          over ? "border-blue-500 bg-blue-50" : "border-slate-300 bg-white hover:border-slate-400")}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); }}
        role="button" tabIndex={0} aria-label="Choose files to upload"
      >
        <p className="text-base font-medium text-slate-800">Drop files here, or click to choose</p>
        <p className="mt-1 text-sm text-slate-500">Several at once is fine.</p>
        <p className="mt-3 text-xs text-slate-500">
          Allowed: <span className="font-mono">{extensions}</span> · up to {info?.max_file_size_mb ?? 200} MB each
        </p>
        <input ref={input} type="file" multiple hidden data-testid="file-input"
          onChange={(e) => { if (e.target.files) add(e.target.files); e.target.value = ""; }} />
      </div>

      {items.length > 0 && (
        <section className="card" aria-live="polite">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="text-slate-600">
              {busy ? <span className="inline-flex items-center gap-1.5"><Spinner /> Uploading…</span> : `${done} of ${items.length} uploaded`}
              {failed > 0 && !busy && <span className="ml-2 text-red-700">{failed} failed</span>}
            </span>
            <span className="flex gap-2">
              {failed > 0 && !busy && <button className="btn" onClick={retryFailed}>Retry failed</button>}
              {!busy && <button className="btn" onClick={() => setItems([])}>Clear list</button>}
              {done > 0 && <Link className="btn btn-primary" href="/documents">View documents</Link>}
            </span>
          </div>
          <ul className="divide-y divide-slate-100">
            {items.map((it) => (
              <li key={it.id} className="py-2.5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium" title={it.file.name}>{it.file.name}</p>
                    <p className="text-xs text-slate-500">{formatBytes(it.file.size)}</p>
                  </div>
                  <div className="shrink-0 text-right text-sm">
                    {it.state === "done" && (
                      <Link href={it.documentId ? `/documents/${it.documentId}` : "/documents"} className="font-medium text-green-700 hover:underline">✓ Uploaded</Link>
                    )}
                    {it.state === "duplicate" && (
                      <Link href={it.documentId ? `/documents/${it.documentId}` : "/documents"} className="font-medium text-amber-700 hover:underline">Already uploaded</Link>
                    )}
                    {it.state === "invalid" && <span className="font-medium text-red-700">Not uploaded</span>}
                    {it.state === "error" && <span className="font-medium text-red-700">Failed</span>}
                    {it.state === "waiting" && <span className="text-slate-500">Waiting…</span>}
                  </div>
                </div>
                {it.state === "uploading" && <div className="mt-1.5"><ProgressBar value={it.progress} label={`Uploading ${it.file.name}`} /></div>}
                {it.message && <p className={cx("mt-1 text-xs", it.state === "duplicate" ? "text-amber-700" : "text-red-700")}>{it.message}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
